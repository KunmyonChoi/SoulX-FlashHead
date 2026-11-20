"""Shared by every test: never the real secrets folder (~/.agentkit holds the owner password and the user's
Google token; a test must neither need nor touch them)."""
import os
import tempfile

os.environ["AGENTKIT_SECRETS_DIR"] = tempfile.mkdtemp(prefix="agentkit-test-secrets-")
