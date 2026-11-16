"""AutoClipper - automatically clips hype moments from live Twitch streams."""
__version__ = "1.1.0"

import warnings

# requests prints a harmless version-check warning with some charset-normalizer releases
warnings.filterwarnings("ignore", message=".*character detection dependency.*")
