"""Validation harness — MILE-050: the statistics that decide acceptance.

VAL-040 (deflated Sharpe, the primary acceptance rule) and its adjacents
live here. Every function fails closed: missing or degenerate input is a
refusal, never an OK-looking default.
"""
