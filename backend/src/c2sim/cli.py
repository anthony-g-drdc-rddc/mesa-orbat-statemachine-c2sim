"""Headless demo, playback, deterministic verification, and server entry point."""

import argparse
import json
from pathlib import Path
from c2sim.contracts.scenario import ControlInput, RunCreateInput, ScenarioSpec
from c2sim.persistence.repository import Repository
from c2sim.runtime.coordinator import Coordinator
from c2sim.runtime.exports import export_run


def main():
    parser = argparse.ArgumentParser(prog="c2sim")
    parser.add_argument("--database", default=".data/c2sim.sqlite")
    sub = parser.add_subparsers(dest="command", required=True)
    demo = sub.add_parser("demo")
    demo.add_argument("--scenario", default="scenarios/coastal-resupply")
    demo.add_argument("--seed", type=int)
    demo.add_argument("--max-steps", type=int, default=500)
    demo.add_argument("--output", default=".data/exports")
    playback = sub.add_parser("playback")
    playback.add_argument("run_id")
    playback.add_argument("--checkpoint", type=int)
    verify = sub.add_parser("verify")
    verify.add_argument("run_id")
    restore = sub.add_parser("restore")
    restore.add_argument("run_id")
    restore.add_argument("checkpoint", type=int)
    serve = sub.add_parser("serve")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=6158)
    args = parser.parse_args()
    if args.command == "serve":
        import uvicorn
        from c2sim.api.app import create_app

        uvicorn.run(create_app(database=args.database), host=args.host, port=args.port, workers=1)
        return
    repository = Repository(args.database)
    try:
        coordinator = Coordinator(repository)
        if args.command == "playback":
            print(json.dumps(repository.load(args.run_id, args.checkpoint), indent=2))
        elif args.command == "restore":
            print(coordinator.restore(args.run_id, args.checkpoint)["run_id"])
        elif args.command == "verify":
            from c2sim.runtime.replay import verify_history

            print(json.dumps(verify_history(repository.history(args.run_id))))
        elif args.command == "demo":
            path = Path(args.scenario)
            spec = ScenarioSpec.model_validate_json((path / "mission.json").read_text())
            config = RunCreateInput.model_validate_json((path / "run-config.json").read_text())
            if args.seed is not None:
                config.decision_provider.seed = args.seed
            state = coordinator.create((path / "source.orbat.json").read_bytes(), spec, config)
            run_id = state["run_id"]
            coordinator.control(
                run_id,
                ControlInput(command_id="demo-start", action="start", expected_control_revision=0),
            )
            for _ in range(args.max_steps):
                state = coordinator.tick(run_id)
                if state["lifecycle"] != "RUNNING":
                    break
            directory, files = export_run(repository, run_id, args.output)
            print(
                json.dumps(
                    {
                        "run_id": run_id,
                        "lifecycle": state["lifecycle"],
                        "mission_state": state["mission"]["state"],
                        "time_ms": state["time_ms"],
                        "exports": [str(directory / f) for f in files],
                    },
                    indent=2,
                )
            )
            if state["lifecycle"] == "RUNNING":
                raise SystemExit("Step limit reached; run remains recoverable in database")
    finally:
        repository.close()


if __name__ == "__main__":
    main()
