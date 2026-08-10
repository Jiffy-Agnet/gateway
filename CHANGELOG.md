# CHANGELOG

<!-- version list -->

## v0.2.0-rc.2 (2026-08-10)

### Features

- **install**: Ensure adequate host swap before starting Jiffy
  ([`9d3f118`](https://github.com/Jiffy-Agnet/gateway/commit/9d3f11851e16c4819fa1937d04b79c984d60a46d))


## v0.2.0-rc.1 (2026-08-10)

### Bug Fixes

- Run the agent exec detached and poll it instead of blocking
  ([`c4e7e11`](https://github.com/Jiffy-Agnet/gateway/commit/c4e7e11737e8ab203acec9daf628ab2caeefcb3c))

- **ci**: Revert setup-uv action version to v6 for compatibility
  ([`0af34d0`](https://github.com/Jiffy-Agnet/gateway/commit/0af34d02cf6390401aee5b666019b99fffb66812))

- **sandbox**: Detach the agent exec and poll it, with a Gateway-enforced timeout
  ([`b4f5e04`](https://github.com/Jiffy-Agnet/gateway/commit/b4f5e04bc5e7f4a507352945287142e5843d5a37))

### Documentation

- Record why the agent exec must stay detached
  ([`9595fd1`](https://github.com/Jiffy-Agnet/gateway/commit/9595fd10083b3455cbb2d81459773bde385b55ae))

### Features

- **sandbox**: Make the agent run window configurable via SANDBOX_AGENT_TIMEOUT
  ([`1348939`](https://github.com/Jiffy-Agnet/gateway/commit/1348939e0a3f0f8e6973fa9ebe3ac6c4bc2ed49e))


## v0.1.2-rc.5 (2026-08-09)


## v0.1.2-rc.4 (2026-08-09)


## v0.1.2-rc.3 (2026-08-09)

### Bug Fixes

- Update JIFFY_IMAGE_VERSION to JIFFY_GATEWAY_IMAGE_VERSION and adjust Docker image references
  ([`75d1dce`](https://github.com/Jiffy-Agnet/gateway/commit/75d1dce7a9d9ffcfe04432b882090081c5357648))


## v0.1.2-rc.2 (2026-08-09)

### Bug Fixes

- Correct syntax for JIFFY_IMAGE_VERSION in .env.example
  ([`eeec7db`](https://github.com/Jiffy-Agnet/gateway/commit/eeec7dbb7c2053d7733c39bd73f31f53445a85af))


## v0.1.2-rc.1 (2026-08-09)

### Bug Fixes

- Read JIFFY_GATEWAY_URL from vars, not secrets
  ([`1ba8793`](https://github.com/Jiffy-Agnet/gateway/commit/1ba8793e3f97ee1cf2e006c35b214d9631a77c42))

### Documentation

- Fix JIFFY_GATEWAY_URL — variable, not secret
  ([`c5b132e`](https://github.com/Jiffy-Agnet/gateway/commit/c5b132e453c782e774305233bfb5edc3cb3b2411))


## v0.1.1 (2026-08-06)


## v0.1.1-rc.1 (2026-08-06)


## v0.1.0-rc.3 (2026-08-05)

### Bug Fixes

- Update Docker Compose file references in README and install script
  ([`c42a2cb`](https://github.com/Jiffy-Agnet/gateway/commit/c42a2cb815336072a4b07e919dd992f6ede6674e))


## v0.1.0-rc.2 (2026-08-04)

### Refactoring

- Update version retrieval to a function and adjust version in uv.lock
  ([`48b0c8b`](https://github.com/Jiffy-Agnet/gateway/commit/48b0c8b76a578ff7aa4478373a7a2de6a0d69634))


## v0.1.0-rc.1 (2026-08-04)

### Bug Fixes

- Update version numbers to 0.0.0 in base.py, views.py, and pyproject.toml
  ([`118b0c7`](https://github.com/javadib/jiffy_gateway/commit/118b0c7dc7f11b3ba0afc47c6064b2bb858da5dc))

### Features

- Configure zero-based semver and per-branch prereleases
  ([`66a9c01`](https://github.com/javadib/jiffy_gateway/commit/66a9c01d76fa3554f09dbde3ec31b270d1921b6f))

- **jiffy-task-planner**: Add optional roadmap-item linking step (Issue #64 follow-up)
  ([`b06309a`](https://github.com/javadib/jiffy_gateway/commit/b06309adc42a44e7f23cc758c57d2cf44c7942df))


## v0.0.0 (2026-08-03)

- Initial Release
