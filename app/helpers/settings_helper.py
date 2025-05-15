import os
import configparser
import re

# Path to the settings.ini file at the project root
INI_PATH = os.path.join(os.getcwd(), 'settings.ini')

# Initialize the ConfigParser and read the ini file if it exists
config = configparser.ConfigParser()
config.read(INI_PATH)

# Function to convert CamelCase names to snake_case for INI keys
def camel_to_snake(name: str) -> str:
    s1 = re.sub(r'(.)([A-Z][a-z]+)', r'\1_\2', name)
    return re.sub(r'([a-z0-9])([A-Z])', r'\1_\2', s1).lower()

def get_default(section: str, option: str, fallback):
    """
    Retrieve the default value for a given section and option from settings.ini.
    Falls back to the provided fallback if the section or option is missing.
    """
    # Normalize option name to snake_case for lookup
    option_key = camel_to_snake(option)
    if not config.has_section(section):
        return fallback
    # Check for snake_case key first, then CamelCase as fallback
    if config.has_option(section, option_key):
        key = option_key
    elif config.has_option(section, option):
        key = option
    else:
        return fallback
    # Determine the type of the fallback to cast appropriately
    if isinstance(fallback, bool):
        try:
            return config.getboolean(section, key, fallback=fallback)
        except ValueError:
            return fallback
    elif isinstance(fallback, int):
        try:
            return config.getint(section, key, fallback=fallback)
        except ValueError:
            return fallback
    elif isinstance(fallback, float):
        try:
            return config.getfloat(section, key, fallback=fallback)
        except ValueError:
            return fallback
    else:
        return config.get(section, key, fallback=str(fallback))
    # Fallback to original default
    return fallback 