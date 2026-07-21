# Detector Interface for Streaming, Control, and Open-source integration (DISCO)
# Copyright (c) 2026, The Regents of the University of California, through
# Lawrence Berkeley National Laboratory (subject to receipt of any required
# approvals from the U.S. Dept. of Energy). All rights reserved.
#
# This software is distributed under a BSD-style license. See the LICENSE.txt
# file in the top-level directory of this distribution for the full terms.

from .socket_server import SocketDataServer

# Export the main class for easy importing
__all__ = ["SocketDataServer"]
