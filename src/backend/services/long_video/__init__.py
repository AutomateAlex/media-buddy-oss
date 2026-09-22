"""Long-video asset pipeline — isolated from the short-video Orchestrator.

LongVideoOrchestrator is a standalone copy of the v2 footage-matching strategy
so long-video iteration (cost/speed/granularity, segmented director) can never
affect the commercial short-video path. Leaf utilities (literal_probe,
observer_judge, footage_service, reframe, compose) stay shared via imports.
"""
