"""sandboxed-devenv: one package installed on every machine that takes part.

The command line lives in ``cli``; each role module registers its own subcommands
there (``control_*`` on the control machine, ``host_*`` on the client host,
``backend_*`` inside backend VMs). PLAN.md describes the layout and the milestones.
"""
