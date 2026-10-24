"""Values more than one part of the relay must agree on, defined once."""

import re

RELAY_PATH = "/api/codelab/relay"
RELAY_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")
# A lowercase DNS name with at least two labels: no port, user info or trailing dot.
HOST_PATTERN = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$")
