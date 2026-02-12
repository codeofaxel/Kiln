## Summary

Brief description of changes and motivation.

## Type of Change

- [ ] Bug fix (non-breaking change that fixes an issue)
- [ ] Feature (non-breaking change that adds functionality)
- [ ] Breaking change (fix or feature that would cause existing functionality to change)
- [ ] Documentation
- [ ] Refactor (no functional changes)
- [ ] Tests

## Testing

Describe what you tested and how.

## Checklist

- [ ] `pytest` passes for affected package(s)
- [ ] No `TODO` in critical paths (print jobs, file upload, temperature control, G-code execution, auth)
- [ ] Docs updated if adding CLI commands, MCP tools, or adapters
- [ ] New adapter implements all `PrinterAdapter` abstract methods (if applicable)
- [ ] Pre-flight checks not bypassed or weakened
- [ ] Temperature and G-code changes validated against safety profiles
