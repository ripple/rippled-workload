import argparse


def main() -> None:
    parser = argparse.ArgumentParser(prog="workload", description="XRPL workload generator")
    subs = parser.add_subparsers(dest="command")

    # --- run (explicit server start) ---
    run_p = subs.add_parser("run", help="Start the workload server")
    run_p.add_argument("--host", default="0.0.0.0", help="Bind address (default: 0.0.0.0)")
    run_p.add_argument("--port", type=int, default=8000, help="Listen port (default: 8000)")

    # --- gen (generate testnet) ---
    gen_p = subs.add_parser("gen", help="Generate testnet configs from config.toml")
    gen_p.add_argument("-o", "--output-dir", default="testnet", help="Output directory (default: testnet)")
    gen_p.add_argument("-v", "--validators", type=int, default=5, help="Number of validators (default: 5)")
    gen_p.add_argument(
        "--amendment-profile",
        default=None,
        help='Amendment profile: "release", "develop", or "custom"',
    )

    args = parser.parse_args()

    match args.command:
        case "gen":
            from workload.gen_cmd import run_gen

            run_gen(
                output_dir=args.output_dir,
                num_validators=args.validators,
                amendment_profile=args.amendment_profile,
            )
        case "run" | None:
            import uvicorn

            host = getattr(args, "host", "0.0.0.0")
            port = getattr(args, "port", 8000)
            uvicorn.run("workload.app:app", host=host, port=port, lifespan="on")


if __name__ == "__main__":
    main()
