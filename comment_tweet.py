import os
import re
import sys
import json
import time
import base64
import random
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

COMMENTED_FILE = "commented.json"

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
# HF CLIENT
# =========================
client = InferenceClient(
    model="meta-llama/Meta-Llama-3-8B-Instruct",
    token=HF_TOKEN
)


# =========================
# RANDOM WAIT
# =========================
def random_wait():
    seconds = random.uniform(6, 12)

    print(
        f"[WAIT] Sleeping for {seconds:.2f} seconds...",
        flush=True
    )

    time.sleep(seconds)


# =========================
# HUMAN TYPE
# =========================
def human_type(locator, text):
    for char in text:
        locator.type(
            char,
            delay=random.randint(30, 120)
        )


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


def _decrypt_payload(
    payload: Dict[str, Any],
    password: str
) -> bytes:

    salt = base64.b64decode(payload["s"])
    nonce = base64.b64decode(payload["n"])
    ciphertext = base64.b64decode(payload["ct"])

    key = _derive_key(
        password.encode("utf-8"),
        salt
    )

    aesgcm = AESGCM(key)

    try:
        return aesgcm.decrypt(
            nonce,
            ciphertext,
            None
        )

    except InvalidTag:
        raise RuntimeError(
            "❌ Decryption failed (InvalidTag)"
        )


def load_cookies(
    file_path: Path
) -> List[Dict[str, Any]]:

    print("[STEP] Loading cookies...", flush=True)

    with file_path.open(
        "r",
        encoding="utf-8"
    ) as f:

        payload = json.load(f)

    plaintext = _decrypt_payload(
        payload,
        DECRYPT_KEY
    )

    cookies = json.loads(
        plaintext.decode("utf-8")
    )

    # normalize SameSite
    for c in cookies:
        if "sameSite" in c:
            val = str(c["sameSite"]).lower()

            if val in [
                "no_restriction",
                "none",
                "unspecified",
                "null"
            ]:
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
# COMMENT GENERATOR
# =========================
def sanitize_text(text):
    return text.replace("**", "").replace("*", "").strip()


def generate_comment(article):
    print(
        "[STEP] Generating AI comment...",
        flush=True
    )

    prompt = (
        f"Generate a viral and engaging X comment under 150 characters.\n"
        f"Rules:\n"
        f"- No religious comment\n"
        f"- No hate or offensive language\n"
        f"- No sentiment hurting\n"
        f"- Should feel human, natural, and opinionated\n"
        f"- React intelligently to the tweet instead of only asking questions\n"
        f"- Add your own rational perspective, observation, agreement, disagreement, or insight related to the tweet\n"
        f"- Questions are allowed but should not dominate the comment\n"
        f"- Sound like a real person joining the conversation naturally\n"
        f"- Add curiosity or strong engagement factor\n"
        f"- Add 2-3 relevant hashtags on next line\n"
        f"- No quotation marks\n"
        f"- No emojis spam\n"
        f"- Make it highly reply-worthy\n"
        f"- Output only the raw final comment text with no labels, prefixes, explanations, asterisks, quotation marks, or extra formatting\n\n"
        f"Tweet:\n{article}"
    )

    for _ in range(MAX_RETRIES):
        try:
            res = client.chat.completions.create(
                messages=[
                    {
                        "role": "user",
                        "content": prompt
                    }
                ],
                max_tokens=120,
                temperature=0.9,
            )

            comment = sanitize_text(
                res.choices[0].message.content
            )

            return comment

        except Exception as e:
            print("[HF ERROR]", e, flush=True)
            time.sleep(5)

    raise RuntimeError("HF failed after retries")


# =========================
# COMMENTED FILE
# =========================
def load_commented():
    if not Path(COMMENTED_FILE).exists():
        return []

    with open(
        COMMENTED_FILE,
        "r",
        encoding="utf-8"
    ) as f:

        return json.load(f)


def save_commented(url):
    data = load_commented()

    data.insert(0, url)

    with open(
        COMMENTED_FILE,
        "w",
        encoding="utf-8"
    ) as f:

        json.dump(
            data,
            f,
            indent=2
        )


# =========================
# MAIN
# =========================
def run():
    print("[START] Script started", flush=True)

    cookies = load_cookies(
        Path(X_COOKIES_FILE)
    )

    print(
        f"[OK] Total cookies loaded: {len(cookies)}",
        flush=True
    )

    # =========================
    # STEALTH SETUP
    # =========================
    stealth = Stealth()

    pw_cm = stealth.use_sync(
        sync_playwright()
    )

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

        print(
            "[STEP] Adding cookies to browser context...",
            flush=True
        )

        context.add_cookies(cookies)

        page = context.new_page()

        print(
            "[OK] Cookies added successfully",
            flush=True
        )

        print("[STEP] Opening X...", flush=True)

        page.goto(
            "https://x.com/home",
            wait_until="domcontentloaded"
        )

        print("[OK] X opened", flush=True)

        random_wait()

        # =========================
        # OPEN FOR YOU PAGE
        # =========================
        print(
            "[STEP] Opening For You page...",
            flush=True
        )

        page.goto(
            "https://x.com/explore/tabs/for_you",
            wait_until="domcontentloaded"
        )

        random_wait()

        # =========================
        # FIND TRENDING TOPIC
        # =========================
        print(
            "[STEP] Finding trending topic...",
            flush=True
        )

        trending_item = page.get_by_role(
            "link",
            name=re.compile(r".*Trending.*")
        ).first

        trend_name = trending_item.inner_text()

        print(
            f"[TREND] {trend_name}",
            flush=True
        )

        random_wait()

        print(
            "[STEP] Clicking trending topic...",
            flush=True
        )

        trending_item.click()

        random_wait()

        # =========================
        # OPEN FIRST TWEET
        # =========================
        print(
            "[STEP] Locating first tweet...",
            flush=True
        )

        tweet_text = page.get_by_role(
            "article"
        ).first.get_by_test_id(
            "tweetText"
        )

        random_wait()

        print(
            "[STEP] Clicking tweet...",
            flush=True
        )

        tweet_text.click()

        random_wait()

        # =========================
        # RECORD URL
        # =========================
        post_url = page.url

        print(
            f"[URL] {post_url}",
            flush=True
        )

        commented = load_commented()

        if post_url in commented:
            print(
                "[INFO] Already commented on this post",
                flush=True
            )

            sys.exit(0)

        # =========================
        # GET ARTICLE TEXT
        # =========================
        print(
            "[STEP] Extracting article text...",
            flush=True
        )

        try:
            article = page.get_by_role(
                "article"
            ).first.get_by_test_id(
                "tweetText"
            ).inner_text()
        except Exception as e:
            print("[ARTICLE ERROR]", e, flush=True)
            sys.exit(1)

        print(
            f"[ARTICLE] {article}",
            flush=True
        )

        if len(article.strip()) < 60:
            print("[INFO] Article too short. Exiting...", flush=True)
            sys.exit(0)

        random_wait()

        # =========================
        # GENERATE COMMENT
        # =========================
        comment = generate_comment(article)

        print(
            f"[COMMENT] {comment}",
            flush=True
        )

        random_wait()

        # =========================
        # LIKE POST
        # =========================
        print(
            "[STEP] Liking Post...",
            flush=True
        )

        like_button = page.get_by_role(
            "button",
            name=re.compile(
                r".*(like|likes).*",
                re.IGNORECASE
            )
        ).first

        like_button.click()

        random_wait()

        # =========================
        # OPEN REPLY POPUP
        # =========================
        print(
            "[STEP] Opening reply popup...",
            flush=True
        )

        reply_button = page.get_by_role(
            "button",
            name=re.compile(
                r".*(reply|replies).*",
                re.IGNORECASE
            )
        ).first

        reply_button.click()

        random_wait()

        # =========================
        # TYPE COMMENT
        # =========================
        print(
            "[STEP] Typing comment...",
            flush=True
        )

        comment_box = page.get_by_role(
            "textbox",
            name="Post text"
        )

        comment_box.click()

        random_wait()

        human_type(
            comment_box,
            comment
        )

        random_wait()

        # =========================
        # POST COMMENT
        # =========================
        print(
            "[STEP] Navigating to Reply button using TAB...",
            flush=True
        )

        for i in range(8):
            page.keyboard.press("Tab")

            print(
                f"[TAB] Pressed TAB {i + 1}/8",
                flush=True
            )

            time.sleep(2)

        print(
            "[STEP] Pressing ENTER to post reply...",
            flush=True
        )

        time.sleep(2)

        page.keyboard.press("Enter")

        random_wait()

        print(
            "[OK] Comment posted successfully",
            flush=True
        )

        # =========================
        # SAVE URL
        # =========================
        save_commented(post_url)

        print(
            "[OK] URL saved to commented.json",
            flush=True
        )

        print(
            "[INFO] Browser will close automatically...",
            flush=True
        )

        time.sleep(random.randint(15, 30))

    except Exception as e:
        print("[ERROR]", e, flush=True)
        sys.exit(1)

    finally:
        try:
            browser.close()
        except:
            pass

        try:
            pw_cm.__exit__(None, None, None)
        except:
            pass

        print("[DONE] Script finished", flush=True)


if __name__ == "__main__":
    run()