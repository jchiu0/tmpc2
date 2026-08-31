import argparse
import importlib
import json


happy_path = importlib.import_module("07_state_machine_happy_path")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Kill one workflow worker and verify another resumes"
    )
    parser.add_argument(
        "--repo", default="https://github.com/jchiu0/scratch1"
    )
    parser.add_argument(
        "--starting-ref",
        default="cursor/create-a-concise-readme-md-expla-386dcb",
    )
    parser.add_argument("--port", type=int, default=8016)
    args = parser.parse_args()
    print(
        json.dumps(
            happy_path.run_scenario(
                repo=args.repo,
                starting_ref=args.starting_ref,
                port=args.port,
                prefix="08_state-machine-recovery",
                crash_run_index=3,
            ),
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
