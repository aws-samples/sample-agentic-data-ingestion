# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Conftest for self-healing agent tests."""

import os
import sys

# Add the agent module to the path
AGENT_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "agentcore", "self_healing_agent")
SHARED_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared")
sys.path.insert(0, AGENT_DIR)
sys.path.insert(0, SHARED_DIR)
