No findings.

The corrected roadmap entry now matches the plan, `AGENTS.md`, and the smoke comment. The doc set is internally consistent on the overlay split, the clean-teardown interpretation of the smoke, and the dedicated `pytest.mark.docker` SIGKILL invariant.

Assessment: ready to ship. Residual risk is only the normal one for this chunk: the implementation depends on real-daemon behavior, but that path is now covered by both the compose smoke and the docker-backed integration tests.