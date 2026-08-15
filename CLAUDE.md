# Resumax

Resumax is a resume tailoring application.

## Architecture

The system separates:
- resume content
- resume layout
- source facts
- tailoring decisions
- validation
- rendering

## Important Principles

- Never fabricate candidate experience.
- Preserve source traceability.
- Prefer deterministic validation where possible.
- LLM output should generally use structured schemas.
- Resume content and resume formatting should remain separate concerns.
- Avoid modifying unrelated modules.

## Python

- Use type hints.
- Prefer Pydantic models for structured application data.
- Keep service logic separate from models.
- Use pytest for tests.

## Development

Before implementing a feature:
- inspect existing abstractions
- avoid duplicating functionality
- understand how the feature fits the overall pipeline

After changes:
- run relevant tests
- explain architectural decisions