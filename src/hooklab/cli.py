"""Start the local development UI from a terminal."""

import argparse
import uvicorn


def main():
    parser = argparse.ArgumentParser(description="Local-first webhook simulator")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=8000, type=int)
    args = parser.parse_args()
    uvicorn.run("hooklab.app:app", host=args.host, port=args.port)


if __name__ == "__main__":
    main()
