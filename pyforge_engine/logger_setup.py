import logging
import coloredlogs
from pathlib import Path

LOGS_DIR = Path(__file__).resolve().parent.parent / "data" / "build_logs"
LOGS_DIR.mkdir(parents=True, exist_ok=True)

def setup_global_logger():
    logger = logging.getLogger("pyforge")
    logger.setLevel(logging.DEBUG)

    # Formatter for the file handler (if uncommented and used)
    file_formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')

    # Configure coloredlogs for console output.
    # It will install its own handler on the 'logger' object.
    # The 'level' argument sets the threshold for the handler created by coloredlogs.
    coloredlogs.install(level='DEBUG', logger=logger, fmt='%(asctime)s %(name)s %(levelname)s %(message)s')

    # File Handler (for general PyForge logs, not build-specific)
    # fh = logging.FileHandler(LOGS_DIR / "pyforge_engine.log")
    # fh.setLevel(logging.DEBUG)
    # fh.setFormatter(file_formatter)
    # logger.addHandler(fh)
    return logger

def get_build_logger(job_name: str, build_id: str):
    """Creates a specific logger for a build instance."""
    build_log_dir = LOGS_DIR / job_name
    build_log_dir.mkdir(parents=True, exist_ok=True)
    log_file_path = build_log_dir / f"{build_id}.log"

    logger = logging.getLogger(f"pyforge.build.{job_name}.{build_id}")
    logger.setLevel(logging.DEBUG)
    # Prevent double logging to parent if console handler is already on parent
    logger.propagate = False


    # File Handler for this specific build
    fh = logging.FileHandler(log_file_path)
    fh.setLevel(logging.DEBUG)
    formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
    fh.setFormatter(formatter)
    logger.addHandler(fh)

    # Optional: also log build output to console if needed for debugging
    # ch_build = logging.StreamHandler()
    # ch_build.setLevel(logging.INFO)
    # ch_build.setFormatter(formatter)
    # logger.addHandler(ch_build)

    return logger, str(log_file_path)

# Initialize global logger
logger = setup_global_logger()
