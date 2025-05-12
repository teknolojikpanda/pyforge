import json
from pathlib import Path
from typing import Dict, Optional, List, Any
import threading # For locks
from .logger_setup import logger # Import the global logger

AGENT_CONFIG_FILE_NAME = "agents_config.json" # Relative to data directory

class AgentInfo:
    def __init__(self, name: str, address: str, tags: Optional[List[str]] = None, max_executors: int = 1):
        self.name = name
        self.address = address  # e.g., http://localhost:8081
        self.tags = tags if tags else []
        self.max_executors: int = max(1, max_executors) # Ensure at least 1 executor
        self.current_executors_used: int = 0
        self.executor_lock: threading.Lock = threading.Lock() # To protect current_executors_used

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "address": self.address,
            "tags": self.tags,
            "max_executors": self.max_executors,
            # Note: current_executors_used is runtime state, not config
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'AgentInfo':
        return cls(
            name=data['name'],
            address=data['address'],
            tags=data.get('tags'),
            max_executors=data.get('max_executors', 1)
        )


class AgentManager:
    def __init__(self, data_root: Path):
        self.config_file = data_root / AGENT_CONFIG_FILE_NAME
        self.agents: Dict[str, AgentInfo] = {}  # name -> AgentInfo
        self.agent_executors: Dict[str, int] = {} # Tracks used executors for each agent
        self.logger = logger # Initialize logger for the AgentManager instance
        self.load_agents()

    def load_agents(self):
        self.agents = {}
        self.agent_executors = {} # Reset used executors on reload
        if not self.config_file.exists():
            self.logger.warning(f"Agent configuration file not found: {self.config_file}. No agents loaded.")
            return

        try:
            with open(self.config_file, 'r') as f:
                # Assuming agents_config.json is a list of agent objects
                agents_data_list = json.load(f) 
            
            if not isinstance(agents_data_list, list):
                 self.logger.error(f"Agent configuration file {self.config_file} is not a list. Skipping.")
                 return

            for agent_config_dict in agents_data_list: 
                try:
                    agent_name = agent_config_dict.get("name")
                    if not agent_name:
                        self.logger.warning(f"Agent configuration missing 'name': {agent_config_dict}. Skipping.")
                        continue
                    
                    # Use AgentInfo.from_dict for parsing and validation
                    agent_info = AgentInfo.from_dict(agent_config_dict)
                    
                    self.agents[agent_info.name] = agent_info
                    # Initialize used executors, preserving if agent was already loaded
                    self.agent_executors.setdefault(agent_info.name, 0) 
                    
                except (ValueError, TypeError) as e_val:
                    self.logger.error(f"Invalid configuration for agent '{agent_config_dict.get('name', 'Unknown')}': {e_val}. Skipping.")
                except Exception as e_gen:
                    self.logger.error(f"Unexpected error loading agent config '{agent_config_dict.get('name', 'Unknown')}': {e_gen}. Skipping.", exc_info=True)

            self.logger.info(f"Loaded {len(self.agents)} agent(s) from {self.config_file}")
        except json.JSONDecodeError:
            self.logger.error(f"Error decoding JSON from agent config file: {self.config_file}")
        except Exception as e:
            self.logger.error(f"Failed to load agents from {self.config_file}: {e}", exc_info=True)

    def get_agent(self, name: str) -> Optional[AgentInfo]:
        """Gets AgentInfo by name, updating runtime stats from agent_executors."""
        agent = self.agents.get(name)
        if agent:
            # Update the runtime stat from the separate tracking dict
            agent.current_executors_used = self.agent_executors.get(name, 0)
        return agent

    def allocate_executor(self, agent_name: str) -> bool:
        """Attempts to allocate an executor slot on the agent. Returns True if successful."""
        agent = self.get_agent(agent_name) # Use get_agent to ensure stats are fresh
        if not agent:
            self.logger.warning(f"Attempted to allocate executor for unknown agent: {agent_name}")
            return False
        
        # Use the lock from the AgentInfo instance
        with agent.executor_lock:
            if agent.current_executors_used < agent.max_executors:
                # Update the separate tracking dict
                self.agent_executors[agent_name] = agent.current_executors_used + 1
                agent.current_executors_used += 1 # Update the instance as well
                self.logger.info(f"Allocated executor for agent {agent_name}. Used: {agent.current_executors_used}/{agent.max_executors}")
                return True
            else:
                self.logger.debug(f"No free executors on agent {agent_name}. Used: {agent.current_executors_used}/{agent.max_executors}")
                return False

    def release_executor(self, agent_name: str):
        """Releases an executor slot on the agent."""
        agent = self.get_agent(agent_name) # Use get_agent to ensure stats are fresh
        if not agent:
            self.logger.warning(f"Attempted to release executor for unknown agent: {agent_name}")
            return
        
        # Use the lock from the AgentInfo instance
        with agent.executor_lock:
            # Update the separate tracking dict
            self.agent_executors[agent_name] = max(0, self.agent_executors.get(agent_name, 0) - 1)
            agent.current_executors_used = self.agent_executors[agent_name] # Update the instance as well
            self.logger.info(f"Released executor for agent {agent_name}. Used: {agent.current_executors_used}/{agent.max_executors}")

    def find_available_agent(self) -> Optional[AgentInfo]:
        """
        Finds the first configured agent that has available executor capacity.
        This method is kept for compatibility or simple "any available" logic.
        For "most available", use find_most_available_agent().
        """
        if not self.agents:
            self.logger.info("No agents configured in the system.")
            return None

        # Iterate through agents and check capacity using the tracking dict
        for agent_name, agent_info in self.agents.items():
             # Use the value from the tracking dict for the check
            current_used = self.agent_executors.get(agent_name, 0)
            if current_used < agent_info.max_executors:
                # Optionally, perform a quick health check here before returning
                # For example, by pinging agent.address/health
                # For now, we assume if it has capacity, it's a candidate.
                self.logger.info(f"Found agent '{agent_name}' with available capacity.")
                # Return the AgentInfo instance (which might have slightly stale current_executors_used)
                # The allocate_executor method will use the tracking dict as the source of truth anyway.
                return agent_info 
        
        self.logger.info("No agents found with available executor capacity.")
        return None

    def find_most_available_agent(self) -> Optional[AgentInfo]:
        """
        Finds the configured agent with the most available executor slots.
        Returns the AgentInfo object or None if no agents are configured or available.
        """
        if not self.agents:
            self.logger.info("No agents configured in the system.")
            return None

        most_available_agent: Optional[AgentInfo] = None
        max_available_slots = -1  # Initialize with a value less than any possible available slots

        for agent_name, agent_info in self.agents.items():
            # Get current usage from the tracking dict
            current_used = self.agent_executors.get(agent_name, 0)
            available_slots = agent_info.max_executors - current_used # Calculate available slots

            if available_slots > max_available_slots:
                max_available_slots = available_slots
                most_available_agent = agent_info

        if most_available_agent and max_available_slots > 0:
            self.logger.info(f"Selected most available agent '{most_available_agent.name}' with {max_available_slots} free slots.")
            # Return the AgentInfo instance
            return most_available_agent
        else:
            self.logger.info("No agents found with available executor capacity (all full or none configured).")
            return None
