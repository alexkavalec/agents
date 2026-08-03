import typer

from agents.application.trade import Trader

app = typer.Typer()


@app.command()
def run_autonomous_trader() -> None:
    """
    Let an autonomous system trade for you.
    """
    import sys
    import traceback
    print("Starting autonomous trader...", flush=True)
    try:
        print("Initializing Trader...", flush=True)
        trader = Trader()
        print("Trader initialized. Running trade...", flush=True)
        trader.one_best_trade()
        print("Trade complete!", flush=True)
    except Exception as e:
        print(f"ERROR: {e}", flush=True)
        traceback.print_exc()
        sys.exit(1)

@app.command()
def run_loop(interval_minutes: int = 30) -> None:
    """
    Run autonomous trader in a continuous loop
    """
    import time
    while True:
        print("Starting trade cycle...")
        try:
            trader = Trader()
            trader.one_best_trade()
            print(f"Trade cycle complete. Sleeping {interval_minutes} minutes...", flush=True)
        except Exception as e:
            print(f"Error in trade cycle: {e}")
        time.sleep(interval_minutes * 60)

if __name__ == "__main__":
    app()
