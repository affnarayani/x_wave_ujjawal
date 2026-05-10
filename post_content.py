import os
import json
import time
import base64
import random
import shutil
import requests
from pathlib import Path
from typing import List, Dict, Any

from dotenv import load_dotenv

from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.exceptions import InvalidTag

from playwright.sync_api import sync_playwright
from huggingface_hub import InferenceClient

from playwright_stealth import Stealth


# =========================
# CONFIG
# =========================
HEADLESS = True

X_COOKIES_FILE = "cookies.json.encrypted"
POSTED_CONTENT_FILE = "posted_content.json"

TEMP_DIR = Path("temp")
TEMP_DIR.mkdir(exist_ok=True)

PBKDF2_ITERATIONS = 200_000
MAX_RETRIES = 3

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"


# =========================
# ENV
# =========================
load_dotenv()

DECRYPT_KEY = os.getenv("DECRYPT_KEY")
HF_TOKEN = os.getenv("HF_TOKEN")

if not DECRYPT_KEY:
    raise RuntimeError("DECRYPT_KEY missing")

if not HF_TOKEN:
    raise RuntimeError("HF_TOKEN missing")


# =========================
# RANDOM WAIT
# =========================
def random_wait():
    seconds = random.uniform(6, 12)
    print(f"[WAIT] Sleeping for {seconds:.2f} seconds...", flush=True)
    time.sleep(seconds)


# =========================
# CRYPTO
# =========================
def _derive_key(password: bytes, salt: bytes) -> bytes:
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=PBKDF2_ITERATIONS,
    )
    return kdf.derive(password)


def _decrypt_payload(payload: Dict[str, Any], password: str) -> bytes:
    salt = base64.b64decode(payload["s"])
    nonce = base64.b64decode(payload["n"])
    ciphertext = base64.b64decode(payload["ct"])

    key = _derive_key(password.encode("utf-8"), salt)
    aesgcm = AESGCM(key)

    try:
        return aesgcm.decrypt(nonce, ciphertext, None)
    except InvalidTag:
        raise RuntimeError("❌ Decryption failed (InvalidTag)")


def load_cookies(file_path: Path) -> List[Dict[str, Any]]:
    print("[STEP] Loading cookies...", flush=True)

    with file_path.open("r", encoding="utf-8") as f:
        payload = json.load(f)

    plaintext = _decrypt_payload(payload, DECRYPT_KEY)
    cookies = json.loads(plaintext.decode("utf-8"))

    # normalize SameSite
    for c in cookies:
        if "sameSite" in c:
            val = str(c["sameSite"]).lower()

            if val in ["no_restriction", "none", "unspecified", "null"]:
                c["sameSite"] = "None"
            elif val == "lax":
                c["sameSite"] = "Lax"
            elif val == "strict":
                c["sameSite"] = "Strict"
            else:
                c["sameSite"] = "Lax"

    print("[OK] Cookies loaded", flush=True)
    return cookies


# =========================
# AI
# =========================
client = InferenceClient(
    model="meta-llama/Meta-Llama-3-8B-Instruct",
    token=HF_TOKEN
)


def sanitize_ai_content(text):
    return text.replace("**", "").replace("*", "").strip()


def rewrite_with_hf(text):
    print("[STEP] Rewriting content with HF...", flush=True)

    prompt = (
        f"Rewrite the legal content below into a high-performing X post in STRICTLY 180 characters or less. Must not exceed 180 characters.\n"
        f"Rules:\n"
        f"- Strong hook\n"
        f"- Professional tone\n"
        f"- SEO friendly\n"
        f"- Add 3-5 relevant hashtags after a new line at end\n"
        f"- No markdown symbols\n"
        f"- No extra commentary\n"
        f"- Do NOT surround the post with quotation marks or inverted commas"
        f"Content: {text}"
    )

    for _ in range(MAX_RETRIES):
        try:
            res = client.chat.completions.create(
                messages=[{"role": "user", "content": prompt}],
                max_tokens=220,
                temperature=0.7,
            )

            result = sanitize_ai_content(
                res.choices[0].message.content
            )

            return result

        except Exception as e:
            print("[AI ERROR]", e, flush=True)
            time.sleep(5)

    return sanitize_ai_content(text)


# =========================
# CONTENT
# =========================
def load_json(url):
    print("[STEP] Fetching content...", flush=True)
    return requests.get(url).json()


def get_new_content():
    url = "https://raw.githubusercontent.com/affnarayani/ninetynine_credits_legal_advice_app_content/main/content.json"

    data = load_json(url)

    posted = []

    if Path(POSTED_CONTENT_FILE).exists():
        posted = json.load(
            open(POSTED_CONTENT_FILE, "r", encoding="utf-8")
        )

    posted_titles = {p["title"] for p in posted}

    for item in data:
        if item["title"] not in posted_titles:
            return item

    return None


def download_image(url, name):
    path = TEMP_DIR / name

    r = requests.get(url, stream=True)

    with open(path, "wb") as f:
        shutil.copyfileobj(r.raw, f)

    return path


# =========================
# MAIN
# =========================
def run():
    print("[START] Script started", flush=True)

    cookies = load_cookies(Path(X_COOKIES_FILE))

    print(f"[OK] Total cookies loaded: {len(cookies)}", flush=True)

    content = get_new_content()

    if not content:
        print("[INFO] No new content", flush=True)
        return

    rewritten = rewrite_with_hf(content["description"])

    image_path = download_image(
        content["image"],
        "post.jpg"
    )

    # =========================
    # STEALTH SETUP
    # =========================
    stealth = Stealth()

    pw_cm = stealth.use_sync(sync_playwright())

    pw = pw_cm.__enter__()

    try:
        browser = pw.chromium.launch(
            headless=HEADLESS,
            args=[
                "--start-maximized",
                "--disable-blink-features=AutomationControlled"
            ]
        )

        context = browser.new_context(
            no_viewport=True,
            user_agent=USER_AGENT
        )

        print("[STEP] Adding cookies to browser context...", flush=True)

        context.add_cookies(cookies)

        page = context.new_page()

        print("[OK] Cookies added successfully", flush=True)

        print("[STEP] Opening X...", flush=True)

        page.goto(
            "https://x.com/home",
            wait_until="domcontentloaded"
        )

        print("[OK] X opened", flush=True)

        random_wait()

        # =========================
        # OPEN POST BOX
        # =========================
        print("[STEP] Clicking new tweet button...", flush=True)

        page.get_by_test_id(
            "SideNav_NewTweet_Button"
        ).click()

        random_wait()

        # =========================
        # WRITE POST
        # =========================
        print("[STEP] Writing tweet...", flush=True)

        tweet_box = page.get_by_role(
            "textbox",
            name="Post text"
        )

        tweet_box.click()

        random_wait()

        tweet_box.fill(rewritten)

        random_wait()

        # =========================
        # UPLOAD IMAGE
        # =========================
        print("[STEP] Uploading image...", flush=True)

        page.locator(
            '[data-testid="fileInput"]'
        ).first.set_input_files(
            str(image_path)
        )

        random_wait()
        # =========================
        # CLICK POST
        # =========================
        print("[STEP] Clicking post button...", flush=True)

        for i in range(13):
            page.keyboard.press("Tab")
            print(f"[TAB] Pressed TAB {i + 1}/13", flush=True)
            time.sleep(2)

        print("[STEP] Pressing ENTER to post...", flush=True)

        time.sleep(2)

        page.keyboard.press("Enter")

        random_wait()

        print("✅ Post published successfully!", flush=True)

        # =========================
        # SAVE POSTED CONTENT
        # =========================
        posted = []

        if Path(POSTED_CONTENT_FILE).exists():
            posted = json.load(
                open(
                    POSTED_CONTENT_FILE,
                    "r",
                    encoding="utf-8"
                )
            )

        posted.insert(0, content)

        with open(
            POSTED_CONTENT_FILE,
            "w",
            encoding="utf-8"
        ) as f:
            json.dump(posted, f, indent=2)

        print(
            "[INFO] Browser will close automatically after 30 seconds...",
            flush=True
        )

        time.sleep(30)

    except Exception as e:
        print("[ERROR]", e, flush=True)

    finally:
        try:
            browser.close()
        except:
            pass

        try:
            if TEMP_DIR.exists():
                shutil.rmtree(TEMP_DIR)

            TEMP_DIR.mkdir(exist_ok=True)

            print("[CLEANUP] Temp cleared", flush=True)

        except Exception as e:
            print("[CLEANUP ERROR]", e, flush=True)

        try:
            pw_cm.__exit__(None, None, None)
        except:
            pass

        print("[DONE] Script finished", flush=True)


if __name__ == "__main__":
    run()