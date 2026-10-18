#!/usr/bin/env python3
"""Repository wrapper for the installed ``tunaxis-backtest`` command."""

from __future__ import annotations

from tunaxis_data.cli.backtest import main

if __name__ == "__main__":
    raise SystemExit(main())
