"""Shared test setup: the deterministic 'baseline' AI profile unless a test chooses another."""
import os

os.environ.setdefault("CONFLICTSCAN_FLEET_PROFILE", "baseline")
# No test may reach a real model server: anything not stubbed fails fast here.
os.environ.setdefault("CONFLICTSCAN_MODEL_SERVERS", '[{"name": "test-null", "url": "http://127.0.0.1:9"}]')
