import sys
from dotenv import load_dotenv

load_dotenv()

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--cli":
        from registry import registry
        prompt = " ".join(sys.argv[2:]) if len(sys.argv) > 2 else "Build a simple weather API in FastAPI."
        print(f"Running in CLI mode: {prompt}")
        registry.run_agent_workflow(prompt, lambda event: print(f"[{event.get('agent', 'System')}] {event.get('type')}: {event.get('text', event.get('message', ''))}"))
    else:
        # Default: Launch LIX PyWebView Desktop UI
        import app
        app.main()