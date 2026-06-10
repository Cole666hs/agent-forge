"""Side-effect-free import contract for the observability package.

Importing agentforge.observability (or any submodule) must NOT:
  - add log handlers to the root logger or any agentforge.* logger
  - create threads
  - open files / sockets
  - initialize the global metrics registry eagerly

This is the line between a library and a service. Adding a check here
when we add new observability features keeps the contract honest.
"""
