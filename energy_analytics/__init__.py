"""Energy disaggregation for the Home Assistant / InfluxDB setup.

Decomposes whole-house P1 consumption into per-device energy using directly
measured loads (solar, smart plugs, heat-pump backup heater), a heat-pump
compressor estimate, and heuristic detectors for the un-metered induction hob
and fridge.
"""
