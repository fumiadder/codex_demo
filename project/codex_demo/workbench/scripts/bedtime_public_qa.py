#!/usr/bin/env python3
"""Post-deployment checks for the existing free bedtime-story test site.

Public: python scripts/bedtime_public_qa.py
Local:  python scripts/bedtime_public_qa.py --self-test

Only the fixed, authorized HTTPS test origin is accepted for public requests.
Two random TEST_ accounts are created; no existing user's records are read.
Passwords, cookies, email addresses, IDs and response bodies are never logged.
Only disabled cloud capabilities are exercised: enabled providers are inspected
without calling generation, enrollment, synthesis or paid web search. No real
recording or fake cloud provider is used. Favorites/history and membership are
cleaned up; current APIs cannot delete the temporary accounts or spaces.
This HTTP smoke test does not verify audio quality, browser rendering, mobile
background playback, persistence across a Render restart or domestic access.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import secrets
import sys
import tempfile
import threading
import time
import wave
from pathlib import Path
from urllib.parse import urlencode, urlsplit

from public_deploy_qa import (
    COOKIE_NAME, DEFAULT_TARGET, Client, QAError, expect, require,
    safety_headers, session_cookie,
)

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from bedtime import HTTPProviders, SLEEP_ENDING, STORY_CATEGORIES, story_paragraphs


def wav_fixture():
    """Bounded non-silent PCM fixture, explicitly not a person's recording."""
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as recording:
        recording.setnchannels(1)
        recording.setsampwidth(2)
        recording.setframerate(24000)
        recording.writeframes(b"\x20\x00" * (10 * 24000))
    return buffer.getvalue()


class BedtimeVerification:
    def __init__(self, target, max_runtime=300, startup_timeout=120):
        self.origin = target
        self.secure = urlsplit(target).scheme == "https"
        self.deadline = time.monotonic() + max_runtime
        self.startup_timeout = startup_timeout
        self.owner, self.member, self.anon = (
            Client(target, self.deadline) for _ in range(3)
        )
        self.stage = "startup"
        self.passes = 0
        self.accounts = []
        self.personal = []
        self.shared = None
        self.member_added = False
        self.favorites = []
        self.histories = []

    def pass_stage(self, label):
        self.passes += 1
        print("PASS " + label, flush=True)

    def api(self, client, space, route, method="GET", payload=None, status=200):
        response = expect(client.request(method, f"/api/spaces/{space}/bedtime/{route}", payload=payload), status)
        safety_headers(response, self.secure, api=True)
        return response.json()

    def startup(self):
        self.stage = "cold-start readiness"
        end = min(self.deadline, time.monotonic() + self.startup_timeout)
        attempts = 0
        while True:
            attempts += 1
            try:
                response = self.anon.request("GET", "/api/health")
                if response.status == 200:
                    require(response.json().get("ok") is True, "health response did not report ready")
                    safety_headers(response, self.secure, api=True)
                    break
                require(response.status in (429, 502, 503, 504), "unexpected health status")
            except QAError as error:
                if str(error) != "network request failed or timed out":
                    raise
            remaining = end - time.monotonic()
            require(remaining > 0 and attempts < 12, "cold-start retry budget exhausted")
            time.sleep(min(2 ** min(attempts - 1, 3), remaining))

        config = expect(self.anon.request("GET", "/api/config"), 200)
        safety_headers(config, self.secure, api=True)
        flags = config.json()
        require(flags.get("testMode") is True and flags.get("storagePersistence") == "ephemeral",
                "target is not the declared ephemeral free test environment; no accounts were created")
        require(flags.get("registrationMode") == "open", "test registration is not open; no accounts were created")

    def static_assets(self):
        self.stage = "new bedtime controls, private upload entry and licensed local font"
        response = expect(self.anon.request("GET", "/bedtime.html?panel=voices"), 200)
        safety_headers(response, self.secure)
        for marker in (b'id="story-generator"', b'id="paragraph-mode"', b'id="reader-pagination"',
                       b'id="sleep-overlay"', b'id="wake-sleep"', b'id="voice-file"',
                       b'value="90"', b'value="piano"', b'/bedtime-fonts.css'):
            require(marker in response.body, "new bedtime-page control was missing")
        script = expect(self.anon.request("GET", "/bedtime.js"), 200)
        safety_headers(script, self.secure)
        require(b'/bedtime.html?panel=voices' in script.body, "generic upload-sharing entry was missing")
        app = expect(self.anon.request("GET", "/app.js"), 200)
        require("睡前小故事".encode() in app.body and b'bedtime-entry-voice' in app.body,
                "life-category bedtime entry was missing")
        for path in ("bedtime-fonts.css", "fonts/zhixu-sleep-sans-sc-v1.woff2",
                     "fonts/LICENSE-NOTO-SANS-SC.txt", "fonts/FONT-SOURCE.json"):
            response = expect(self.anon.request("GET", "/" + path), 200)
            safety_headers(response, self.secure)
            expected = (ROOT / "public" / path).read_bytes()
            require(hashlib.sha256(response.body).digest() == hashlib.sha256(expected).digest(),
                    "deployed font asset or license differed from checked-out source")
        self.pass_stage(self.stage)

    def registration(self):
        self.stage = "disposable test identities and authenticated bedtime access"
        prefix = "TEST_" + secrets.token_hex(10)
        credentials = [{"email": f"{prefix.lower()}_{suffix}@example.invalid",
                        "password": secrets.token_urlsafe(24), "name": prefix + "_" + suffix}
                       for suffix in ("owner", "member")]
        for client, credential in zip((self.owner, self.member), credentials):
            response = expect(client.request("POST", "/api/auth/register", payload=credential), 201)
            safety_headers(response, self.secure, api=True)
            session_cookie(response, client, self.secure)
            user = response.json().get("user", {})
            require(isinstance(user.get("id"), str) and user.get("email") == credential["email"],
                    "test registration identity was invalid")
            self.accounts.append(user["id"])
            spaces = expect(client.request("GET", "/api/spaces"), 200).json().get("spaces", [])
            require(len(spaces) == 1 and spaces[0].get("role") == "owner", "test personal space was invalid")
            self.personal.append(spaces[0]["id"])
            self.api(client, self.personal[-1], "config")
        self.member_email = credentials[1]["email"]
        expect(self.anon.request("GET", f"/api/spaces/{self.personal[0]}/bedtime/config"), 401)
        expect(self.member.request("GET", f"/api/spaces/{self.personal[0]}/bedtime/favorites"), 404)
        self.pass_stage(self.stage)

    def originals(self):
        self.stage = "14 complete originals, six categories, duration and explicit child-age filters"
        raw = json.loads((ROOT / "stories_data.json").read_text(encoding="utf-8"))
        expected = {entry["id"]: entry for entry in raw}
        stories = self.api(self.owner, self.personal[0], "search?scope=local").get("stories")
        require(isinstance(stories, list) and len(stories) == len(expected) == 14,
                "original story catalog did not contain the expected 14 stories")
        require({story.get("id") for story in stories} == set(expected), "original story identifiers differed")
        for story in stories:
            source = expected[story["id"]]
            text = source["text"].rstrip()
            if not text.endswith(SLEEP_ENDING):
                text += "\n\n" + SLEEP_ENDING
            require(story.get("text") == text and story.get("fullText") is True
                    and story.get("isExcerpt") is False and story.get("source", {}).get("kind") == "original",
                    "original story text or provenance was incomplete")
            require(story.get("ending") == SLEEP_ENDING and text.count(SLEEP_ENDING) == 1
                    and story.get("paragraphs") == story_paragraphs(text, SLEEP_ENDING),
                    "original paragraphs or quiet ending were invalid")
        self.story = stories[0]
        require(self.api(self.owner, self.personal[0], "stories/" + self.story["id"]).get("story") == self.story,
                "single-story read differed from catalog text")
        for category in STORY_CATEGORIES:
            selected = self.api(self.owner, self.personal[0], "search?" + urlencode({"category": category})).get("stories", [])
            matches = {story["id"] for story in stories
                       if category == story["category"] or category in story.get("categoryTags", [])}
            require(matches and {story.get("id") for story in selected} == matches, "category filter was invalid")
        for duration in (1, 5):
            selected = self.api(self.owner, self.personal[0], f"search?durationMinutes={duration}").get("stories", [])
            matches = {story["id"] for story in stories if story["readMinutes"] == duration}
            require(matches and {story.get("id") for story in selected} == matches, "duration filter was invalid")
        for age in ("3-6", "7-10"):
            selected = self.api(self.owner, self.personal[0], "search?" + urlencode({"ageGroup": age})).get("stories", [])
            matches = {story["id"] for story in stories if age in story.get("ageGroups", [])}
            require(matches and {story.get("id") for story in selected} == matches,
                    "child-age filter included an ungraded story or omitted a graded story")
        self.pass_stage(self.stage)

    def save(self, client, space, story, position):
        # Register cleanup before the write so a lost response is cleaned too.
        self.favorites.append((client, space, story["id"]))
        saved = self.api(client, space, "favorites/" + story["id"], "PUT", {"story": story}).get("story")
        self.histories.append((client, space))
        self.api(client, space, "history", "POST", {"storyId": story["id"], "story": story,
                 "voiceId": "system:default", "positionSeconds": position})
        return saved

    def snapshots(self):
        self.stage = "private complete-text favorites and resumable history snapshots"
        source = {"id": "test-" + secrets.token_hex(12), "title": "Temporary QA text fixture",
                  "text": "A quiet test paragraph.\n\nAnother quiet test paragraph.\n\n" + SLEEP_ENDING,
                  "category": "自存", "ending": SLEEP_ENDING,
                  "questions": [{"afterParagraph": 0, "question": "A gentle optional test question?"}]}
        self.saved = self.save(self.owner, self.personal[0], source, 37)
        require(self.saved.get("source", {}).get("kind") == "imported",
                "saved test text claimed original or AI provenance")
        require(self.saved.get("text") == source["text"] and self.saved.get("ending") == SLEEP_ENDING
                and self.saved.get("paragraphs") == story_paragraphs(source["text"], SLEEP_ENDING)
                and self.saved.get("questions") == source["questions"], "saved text snapshot lost reading metadata")
        favorites = self.api(self.owner, self.personal[0], "favorites").get("favorites", [])
        history = self.api(self.owner, self.personal[0], "history").get("history", [])
        require(len(favorites) == len(history) == 1 and favorites[0].get("text") == self.saved["text"],
                "test favorites or history did not round-trip")
        require(history[0].get("story") == self.saved and history[0].get("positionSeconds") == 37
                and history[0].get("voiceId") == "system:default", "resume snapshot was invalid")
        require(self.api(self.member, self.personal[1], "favorites").get("favorites") == []
                and self.api(self.member, self.personal[1], "history").get("history") == [],
                "test account read another account's saved data")
        self.pass_stage(self.stage)

    def collaboration(self):
        self.stage = "space and account separation, read-only sharing and immediate revocation"
        response = expect(self.owner.request("POST", "/api/spaces", payload={"name": "TEST_" + secrets.token_hex(10)}), 201)
        self.shared = response.json().get("space", {}).get("id")
        require(isinstance(self.shared, str), "test shared-space response was invalid")
        require(self.api(self.owner, self.shared, "favorites").get("favorites") == [],
                "private-space favorite leaked into a new space")
        self.api(self.owner, self.shared, "stories/" + self.saved["id"], status=404)
        expect(self.member.request("GET", f"/api/spaces/{self.shared}/bedtime/history"), 404)
        self.member_added = True
        expect(self.owner.request("POST", f"/api/spaces/{self.shared}/members",
               payload={"email": self.member_email, "role": "editor"}), 201)
        self.save(self.owner, self.shared, self.saved, 9)
        require(self.api(self.member, self.shared, "favorites").get("favorites") == []
                and self.api(self.member, self.shared, "history").get("history") == [],
                "shared-space member read owner's private bedtime archives")
        self.api(self.member, self.shared, "stories/" + self.saved["id"], status=404)
        different = {**self.saved, "title": "Second account temporary text", "text": "Independent account text."}
        different.pop("ending", None)
        different.pop("questions", None)
        member_saved = self.save(self.member, self.shared, different, 4)
        require(self.api(self.owner, self.shared, "stories/" + self.saved["id"]).get("story") == self.saved
                and self.api(self.member, self.shared, "stories/" + self.saved["id"]).get("story") == member_saved,
                "same story ID collided across accounts")
        require(self.api(self.owner, self.shared, "voices").get("voices") == []
                and self.api(self.member, self.shared, "voices").get("voices") == [],
                "new test identities had unexpected private voice profiles")
        expect(self.owner.request("POST", f"/api/spaces/{self.shared}/members",
               payload={"email": self.member_email, "role": "viewer"}), 201)
        self.api(self.member, self.shared, "search?scope=local")
        self.api(self.member, self.shared, "generate", "POST", {}, 403)
        self.api(self.member, self.shared, "favorites/" + self.story["id"], "PUT", {}, 403)
        self.api(self.member, self.shared, "history", "POST", {"storyId": self.story["id"]}, 403)
        # Restore the disposable editor so its own records can be deleted before
        # the member is removed. This does not affect any existing account.
        expect(self.owner.request("POST", f"/api/spaces/{self.shared}/members",
               payload={"email": self.member_email, "role": "editor"}), 201)
        self.api(self.member, self.shared, "favorites/" + self.saved["id"], "DELETE")
        self.favorites.remove((self.member, self.shared, self.saved["id"]))
        self.api(self.member, self.shared, "history", "DELETE")
        self.histories.remove((self.member, self.shared))
        expect(self.owner.request("DELETE", f"/api/spaces/{self.shared}/members/{self.accounts[1]}"), 200)
        self.member_added = False
        for route in ("config", "favorites", "history", "voices"):
            expect(self.member.request("GET", f"/api/spaces/{self.shared}/bedtime/{route}"), 404)
        self.pass_stage(self.stage)

    def capabilities(self):
        self.stage = "honest disconnected cloud responses without paid provider calls"
        config = self.api(self.owner, self.personal[0], "config")
        require(config.get("systemSpeech") is True, "device speech capability was missing")
        generation = config.get("storyGeneration", {})
        require(generation.get("categories") == STORY_CATEGORIES and generation.get("ageGroups") == ["3-6", "7-10"]
                and generation.get("durationMinutes") == [1, 5], "new generation schema was missing")
        calls = [
            ("storyGeneration", "generate", "POST", {"ageGroup": "3-6", "category": "儿童睡前",
                 "durationMinutes": 1, "style": "healing", "moral": True,
                 "blockScary": True, "protagonist": "rabbit", "interactive": False}),
            ("webSearch", "search?" + urlencode({"scope": "web", "q": "睡前故事"}), "GET", None),
            ("voiceClone", "voices", "POST", None),
            ("synthesis", "synthesize", "POST", {"text": "Temporary QA text.", "voiceId": "cloud:Seren"}),
        ]
        skipped = 0
        for capability, route, method, payload in calls:
            enabled = config.get(capability, {}).get("enabled")
            require(type(enabled) is bool, "cloud capability flag was invalid")
            if enabled:
                skipped += 1
                continue
            if capability == "voiceClone":
                payload = {"name": "TEST_ synthetic PCM fixture", "consent": True,
                           "mimeType": "audio/wav", "audioBase64": base64.b64encode(wav_fixture()).decode()}
            result = self.api(self.owner, self.personal[0], route, method, payload, 503)
            require(isinstance(result.get("error"), str) and bool(result["error"]),
                    "disconnected provider response did not explain its status")
        if skipped:
            print(f"NOTE {skipped} enabled cloud capability/capabilities were read only; no paid calls were made.", flush=True)
        require(self.api(self.owner, self.personal[0], "voices").get("voices") == [],
                "disconnected clone request created a voice profile")
        self.pass_stage(self.stage)

    def cleanup(self):
        failures = 0
        # A bounded cleanup allowance survives a request-budget expiry. This is
        # still limited to records created by these two random test identities.
        for client in (self.owner, self.member):
            client.deadline = max(client.deadline, time.monotonic() + 30)
        if self.member_added and self.shared:
            try:
                expect(self.owner.request("POST", f"/api/spaces/{self.shared}/members",
                       payload={"email": self.member_email, "role": "editor"}), 201)
            except QAError:
                failures += 1
        for client, space, story_id in reversed(self.favorites):
            try:
                self.api(client, space, "favorites/" + story_id, "DELETE")
            except QAError:
                failures += 1
        for client, space in self.histories:
            try:
                self.api(client, space, "history", "DELETE")
            except QAError:
                failures += 1
        for client, space in {(client, space) for client, space, _ in self.favorites} | set(self.histories):
            try:
                require(self.api(client, space, "favorites").get("favorites") == []
                        and self.api(client, space, "history").get("history") == [], "created bedtime records remained")
            except QAError:
                failures += 1
        if self.member_added and self.shared and len(self.accounts) > 1:
            try:
                expect(self.owner.request("DELETE", f"/api/spaces/{self.shared}/members/{self.accounts[1]}"), 200)
            except QAError:
                failures += 1
        for client in (self.owner, self.member):
            if any(cookie.name == COOKIE_NAME for cookie in client.jar):
                try:
                    expect(client.request("POST", "/api/auth/logout"), 200)
                    expect(client.request("GET", "/api/me"), 401)
                except QAError:
                    failures += 1
        if failures:
            print(f"FAIL bedtime cleanup: {failures} operation(s) did not complete", flush=True)
        elif self.accounts:
            self.pass_stage("created favorites/history removed, test membership removed and sessions logged out")
        if self.accounts:
            print("NOTE Temporary TEST_ accounts and spaces remain: the current APIs cannot delete them.", flush=True)
        return failures == 0

    def run(self):
        success = False
        try:
            self.startup()
            self.static_assets()
            self.registration()
            self.originals()
            self.snapshots()
            self.collaboration()
            self.capabilities()
            success = True
        except QAError as error:
            print(f"FAIL {self.stage}: {error}", flush=True)
        except Exception as error:
            print(f"FAIL {self.stage}: unexpected {type(error).__name__}", flush=True)
        finally:
            cleanup_ok = self.cleanup()
        success = success and cleanup_ok
        label = "HTTPS public" if self.secure else "local HTTP self-test"
        print(f"{'PASS' if success else 'FAIL'} SUMMARY: {self.passes} bedtime groups passed ({label}); no paid cloud calls.", flush=True)
        if not self.secure:
            print("NOTE Local self-test skips Secure-cookie/HSTS checks; public HTTPS enforces them.", flush=True)
        return 0 if success else 1


def self_test(args):
    from server import FolioServer
    with tempfile.TemporaryDirectory(prefix="zhixu-bedtime-public-qa-") as data:
        server = FolioServer(("127.0.0.1", 0), data_dir=data, public_dir=ROOT / "public",
                             trust_proxy=False, allowed_origins="", registration_mode="open",
                             storage_persistence="ephemeral")
        server.test_mode = True
        # Real HTTP provider implementation with empty configuration. Never a
        # fake provider, and no inherited administrator keys reach this test.
        server.bedtime_provider = HTTPProviders({})
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            return BedtimeVerification(f"http://127.0.0.1:{server.server_port}", args.max_runtime, args.startup_timeout).run()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--self-test", action="store_true", help="Isolated temporary local HTTP server; no public requests.")
    parser.add_argument("--startup-timeout", type=int, default=120, help="Cold-start budget in seconds (30–180).")
    parser.add_argument("--max-runtime", type=int, default=300, help="Overall request budget in seconds (60–480).")
    args = parser.parse_args()
    try:
        require(30 <= args.startup_timeout <= 180 and 60 <= args.max_runtime <= 480,
                "timeout options are outside their permitted bounds")
        if args.self_test:
            return self_test(args)
        # No TARGET_URL override or public --target flag: account-creating
        # requests stay on the one service authorized for this workflow.
        return BedtimeVerification(DEFAULT_TARGET, args.max_runtime, args.startup_timeout).run()
    except QAError as error:
        print("FAIL setup: " + str(error), flush=True)
        return 1
    except Exception as error:
        print("FAIL setup: " + type(error).__name__, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
