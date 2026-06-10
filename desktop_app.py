import threading
import time
import webbrowser

import uvicorn


HOST = "127.0.0.1"
PORT = 8010
URL = f"http://{HOST}:{PORT}"


def main() -> None:
    thread = threading.Thread(target=run_server, daemon=True)
    thread.start()
    time.sleep(1.5)

    try:
        import webview

        webview.create_window("Video Pipeline", URL, width=1280, height=820)
        webview.start()
    except Exception:
        webbrowser.open(URL)
        keep_alive_console()


def run_server() -> None:
    uvicorn.run("pipeline_web.main:app", host=HOST, port=PORT, log_level="info")


def keep_alive_console() -> None:
    print(f"Video Pipeline is running at {URL}")
    print("Close this window or press Ctrl+C to stop.")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
