# HealthMes Agent

HealthMes Agent is a Hermes-based personal health and wearable intelligence agent.

The project is designed to collect, organize, and reuse personalized information from wearable devices as agent skills. The goal is to let an agent build durable context from a user's wearable signals, routines, and health-adjacent history, then retrieve that context through skill-based workflows.

## Direction

- Build on the Hermes Agent base for agent runtime, skill execution, memory, tool use, and multi-channel operation.
- Accumulate personalized wearable-device information over time.
- Convert repeated health and lifestyle workflows into reusable skills.
- Support future integrations with wearable data sources and open wearable research tooling.

## Repository layout

- `vendor/hermes-agent/` contains the imported Hermes Agent base.
- `vendor/open-wearables/` contains the imported open-wearables reference implementation.
- Root-level project files describe the HealthMes Agent purpose, licensing, and third-party notices.

## References

This project is based on and references:

- Hermes Agent: https://github.com/NousResearch/hermes-agent
- open-wearables: https://github.com/the-momentum/open-wearables

The open-wearables code is kept in a separate folder so wearable data integration work can be developed without mixing it into the Hermes runtime base.

## License

HealthMes Agent is available for non-commercial use under the project license in `LICENSE`.

Commercial use requires a separate paid commercial license from the project owner. See `LICENSE` for details.

This repository includes code derived from Hermes Agent by Nous Research and open-wearables by Momentum, both released under the MIT License. Original notices are preserved in `THIRD_PARTY_NOTICES.md`.
