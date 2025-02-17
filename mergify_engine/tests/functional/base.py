# -*- encoding: utf-8 -*-
#
# Copyright © 2018 Mehdi Abaakouk <sileht@sileht.net>
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.
import copy
import json
import queue
import os
import re
import shutil
import subprocess
import time
import uuid

import daiquiri

import fixtures

import github

import requests
import requests.sessions

import testtools

import vcr

from mergify_engine import branch_updater
from mergify_engine import config
from mergify_engine import duplicate_pull
from mergify_engine import sub_utils
from mergify_engine import utils
from mergify_engine import web
from mergify_engine import worker

LOG = daiquiri.getLogger(__name__)
RECORD = bool(os.getenv("MERGIFYENGINE_RECORD", False))
CASSETTE_LIBRARY_DIR_BASE = "zfixtures/cassettes"
FAKE_DATA = "whatdataisthat"
FAKE_HMAC = utils.compute_hmac(FAKE_DATA.encode("utf8"))

# NOTE(sileht): Celery magic, this just skip amqp and execute tasks directly
# So all REST API calls will block and execute celery tasks directly
worker.app.conf.task_always_eager = True
worker.app.conf.task_eager_propagates = True


class GitterRecorder(utils.Gitter):
    def __init__(self, cassette_library_dir, suffix):
        super(GitterRecorder, self).__init__()
        self.cassette_path = os.path.join(cassette_library_dir, "git-%s.json" % suffix)
        if RECORD:
            self.records = []
        else:
            self.load_records()

    def load_records(self):
        if not os.path.exists(self.cassette_path):
            raise RuntimeError("Cassette %s not found" % self.cassette_path)
        with open(self.cassette_path, "rb") as f:
            data = f.read().decode("utf8")
            self.records = json.loads(data)

    def save_records(self):
        with open(self.cassette_path, "wb") as f:
            data = json.dumps(self.records)
            f.write(data.encode("utf8"))

    def __call__(self, *args, **kwargs):
        if RECORD:
            try:
                out = super(GitterRecorder, self).__call__(*args, **kwargs)
            except subprocess.CalledProcessError as e:
                self.records.append(
                    {
                        "args": self.prepare_args(args),
                        "kwargs": self.prepare_kwargs(kwargs),
                        "exc": {
                            "returncode": e.returncode,
                            "cmd": e.cmd,
                            "output": e.output.decode("utf8"),
                        },
                    }
                )
                raise
            else:
                self.records.append(
                    {
                        "args": self.prepare_args(args),
                        "kwargs": self.prepare_kwargs(kwargs),
                        "out": out.decode("utf8"),
                    }
                )
            return out
        else:
            r = self.records.pop(0)
            if "exc" in r:
                raise subprocess.CalledProcessError(
                    returncode=r["exc"]["returncode"],
                    cmd=r["exc"]["cmd"],
                    output=r["exc"]["output"].encode("utf8"),
                )
            else:
                assert r["args"] == self.prepare_args(args), "%s != %s" % (
                    r["args"],
                    self.prepare_args(args),
                )
                assert r["kwargs"] == self.prepare_kwargs(kwargs), "%s != %s" % (
                    r["kwargs"],
                    self.prepare_kwargs(kwargs),
                )
                return r["out"].encode("utf8")

    def prepare_args(self, args):
        return [arg.replace(self.tmp, "/tmp/mergify-gitter<random>") for arg in args]

    @staticmethod
    def prepare_kwargs(kwargs):
        if "input" in kwargs:
            kwargs["input"] = re.sub(
                r"://[^@]*@", "://<TOKEN>:@", kwargs["input"].decode("utf8")
            )
        return kwargs

    def cleanup(self):
        super(GitterRecorder, self).cleanup()
        if RECORD:
            self.save_records()


class EventReader:
    def __init__(self, app):
        self._app = app
        self._session = requests.Session()
        self._session.trust_env = False
        self._handled_events = queue.Queue()
        self._counter = 0

    def drain(self):
        # NOTE(sileht): Drop any pending events still on the server
        r = self._session.delete(
            "https://gh.mergify.io/events-testing",
            data=FAKE_DATA,
            headers={"X-Hub-Signature": "sha1=" + FAKE_HMAC},
        )
        r.raise_for_status()

    def wait_for(self, event_type, expected_payload, timeout=60 if RECORD else 2):
        started_at = time.monotonic()
        while time.monotonic() - started_at < timeout:
            try:
                event = self._handled_events.get(block=False)
            except queue.Empty:
                for event in self._get_events():
                    self._forward_to_engine(event)
                    self._handled_events.put(event)
                else:
                    if RECORD:
                        time.sleep(1)
                continue

            if event["type"] == event_type and self._match(
                event["payload"], expected_payload
            ):
                return

        raise Exception(
            f"Never got event `{event_type}` with payload `{expected_payload}` (timeout)"
        )

    @classmethod
    def _match(cls, data, expected_data):
        if isinstance(expected_data, dict):
            for key, expected in expected_data.items():
                if key not in data:
                    return False
                if not cls._match(data[key], expected):
                    return False
            return True
        else:
            return data == expected_data

    def _get_events(self):
        # NOTE(sileht): we use a counter to make each call unique in cassettes
        self._counter += 1
        return self._session.get(
            f"https://gh.mergify.io/events-testing?counter={self._counter}",
            data=FAKE_DATA,
            headers={"X-Hub-Signature": "sha1=" + FAKE_HMAC},
        ).json()

    def _forward_to_engine(self, event):
        payload = event["payload"]
        if event["type"] in ["check_run", "check_suite"]:
            extra = "/%s/%s" % (
                payload[event["type"]].get("status"),
                payload[event["type"]].get("conclusion"),
            )
        elif event["type"] == "status":
            extra = "/%s" % payload.get("state")
        else:
            extra = ""
        LOG.warning(
            "### EVENT RECEIVED ### [%s] %s/%s%s: %s",
            event["id"],
            event["type"],
            payload.get("action"),
            extra,
            self._remove_useless_links(copy.deepcopy(event)),
        )
        r = self._app.post(
            "/event",
            headers={
                "X-GitHub-Event": event["type"],
                "X-GitHub-Delivery": "123456789",
                "X-Hub-Signature": "sha1=whatever",
                "Content-type": "application/json",
            },
            data=json.dumps(payload),
        )
        return r

    @classmethod
    def _remove_useless_links(cls, data):
        if isinstance(data, dict):
            data.pop("installation", None)
            data.pop("sender", None)
            data.pop("repository", None)
            data.pop("base", None)
            data.pop("head", None)
            data.pop("id", None)
            data.pop("node_id", None)
            data.pop("tree_id", None)
            data.pop("_links", None)
            data.pop("user", None)
            data.pop("body", None)
            data.pop("after", None)
            data.pop("before", None)
            data.pop("app", None)
            data.pop("timestamp", None)
            data.pop("external_id", None)
            if "organization" in data:
                data["organization"].pop("description", None)
            if "check_run" in data:
                data["check_run"].pop("checks_suite", None)
            for key, value in list(data.items()):
                if key.endswith("url"):
                    del data[key]
                elif key.endswith("_at"):
                    del data[key]
                else:
                    data[key] = cls._remove_useless_links(value)
            return data
        elif isinstance(data, list):
            return [cls._remove_useless_links(elem) for elem in data]
        else:
            return data


class FunctionalTestBase(testtools.TestCase):
    def setUp(self):
        super(FunctionalTestBase, self).setUp()
        self.pr_counter = 0
        self.git_counter = 0
        self.cassette_library_dir = os.path.join(
            CASSETTE_LIBRARY_DIR_BASE, self._testMethodName
        )

        # Recording stuffs
        if RECORD:
            if os.path.exists(self.cassette_library_dir):
                shutil.rmtree(self.cassette_library_dir)
            os.makedirs(self.cassette_library_dir)

        self.recorder = vcr.VCR(
            cassette_library_dir=self.cassette_library_dir,
            record_mode="all" if RECORD else "none",
            match_on=["method", "uri"],
            filter_headers=[
                ("Authorization", "<TOKEN>"),
                ("X-Hub-Signature", "<SIGNATURE>"),
                ("User-Agent", None),
                ("Accept-Encoding", None),
                ("Connection", None),
            ],
            before_record_response=self.response_filter,
            custom_patches=(
                (github.MainClass, "HTTPSConnection", vcr.stubs.VCRHTTPSConnection),
            ),
        )

        self.useFixture(
            fixtures.MockPatchObject(
                branch_updater.utils, "Gitter", lambda: self.get_gitter()
            )
        )

        self.useFixture(
            fixtures.MockPatchObject(
                duplicate_pull.utils, "Gitter", lambda: self.get_gitter()
            )
        )

        # Web authentification always pass
        self.useFixture(fixtures.MockPatch("hmac.compare_digest", return_value=True))

        reponame_path = os.path.join(self.cassette_library_dir, "reponame")

        if RECORD:
            REPO_UUID = str(uuid.uuid4())
            with open(reponame_path, "w") as f:
                f.write(REPO_UUID)
        else:
            with open(reponame_path, "r") as f:
                REPO_UUID = f.read()

        self.name = "repo-%s-%s" % (REPO_UUID, self._testMethodName)

        utils.setup_logging()
        config.log()

        self.git = self.get_gitter()
        self.addCleanup(self.git.cleanup)

        web.app.testing = True
        self.app = web.app.test_client()

        # NOTE(sileht): Prepare a fresh redis
        self.redis = utils.get_redis_for_cache()
        self.redis.flushall()
        self.subscription = {
            "tokens": {"mergifyio-testing": config.MAIN_TOKEN},
            "subscription_active": False,
            "subscription_cost": 100,
            "subscription_reason": "You're not nice",
        }
        self.redis.set(
            "subscription-cache-%s" % config.INSTALLATION_ID,
            sub_utils._encrypt(self.subscription),
        )

        # Let's start recording
        cassette = self.recorder.use_cassette("http.json")
        cassette.__enter__()
        self.addCleanup(cassette.__exit__)

        integration = github.GithubIntegration(
            config.INTEGRATION_ID, config.PRIVATE_KEY
        )
        self.installation_token = integration.get_access_token(
            config.INSTALLATION_ID
        ).token

        self.g_integration = github.Github(
            self.installation_token, base_url="https://api.%s" % config.GITHUB_DOMAIN
        )
        self.g_admin = github.Github(
            config.MAIN_TOKEN, base_url="https://api.%s" % config.GITHUB_DOMAIN
        )
        self.g_fork = github.Github(
            config.FORK_TOKEN, base_url="https://api.%s" % config.GITHUB_DOMAIN
        )

        self.o_admin = self.g_admin.get_organization(config.TESTING_ORGANIZATION)
        self.o_integration = self.g_integration.get_organization(
            config.TESTING_ORGANIZATION
        )
        self.u_fork = self.g_fork.get_user()
        assert self.o_admin.login == "mergifyio-testing"
        assert self.o_integration.login == "mergifyio-testing"
        assert self.u_fork.login == "mergify-test2"

        self.r_o_admin = self.o_admin.create_repo(self.name)
        self.r_o_integration = self.o_integration.get_repo(self.name)
        self.url_main = "https://%s/%s" % (
            config.GITHUB_DOMAIN,
            self.r_o_integration.full_name,
        )
        self.url_fork = "https://%s/%s/%s" % (
            config.GITHUB_DOMAIN,
            self.u_fork.login,
            self.r_o_integration.name,
        )

        # Limit installations/subscription API to the test account
        install = {
            "id": config.INSTALLATION_ID,
            "target_type": "Org",
            "account": {"login": "mergifyio-testing"},
        }

        self.useFixture(
            fixtures.MockPatch(
                "mergify_engine.utils.get_installations", lambda integration: [install]
            )
        )

        real_get_subscription = sub_utils.get_subscription

        def fake_retrieve_subscription_from_db(install_id):
            if int(install_id) == config.INSTALLATION_ID:
                return self.subscription
            else:
                return {
                    "tokens": {},
                    "subscription_active": False,
                    "subscription_reason": "We're just testing",
                }

        def fake_subscription(r, install_id):
            if int(install_id) == config.INSTALLATION_ID:
                return real_get_subscription(r, install_id)
            else:
                return {
                    "tokens": {},
                    "subscription_active": False,
                    "subscription_reason": "We're just testing",
                }

        self.useFixture(
            fixtures.MockPatch(
                "mergify_engine.branch_updater.sub_utils.get_subscription",
                side_effect=fake_subscription,
            )
        )

        self.useFixture(
            fixtures.MockPatch(
                "mergify_engine.branch_updater.sub_utils._retrieve_subscription_from_db",
                side_effect=fake_retrieve_subscription_from_db,
            )
        )

        self.useFixture(
            fixtures.MockPatch(
                "mergify_engine.sub_utils.get_subscription",
                side_effect=fake_subscription,
            )
        )

        self.useFixture(
            fixtures.MockPatch(
                "github.MainClass.Installation.Installation.get_repos",
                return_value=[self.r_o_integration],
            )
        )
        self._event_reader = EventReader(self.app)
        self._event_reader.drain()

    def tearDown(self):
        # self.r_o_admin.delete()
        super(FunctionalTestBase, self).tearDown()

        # NOTE(sileht): Wait a bit to ensure all remaining events arrive. And
        # also to avoid the "git clone fork" failure that Github returns when
        # we create repo too quickly
        if RECORD:
            time.sleep(0.5)

        self._event_reader.drain()

    def wait_for(self, *args, **kwargs):
        return self._event_reader.wait_for(*args, **kwargs)

    def get_gitter(self):
        self.git_counter += 1
        return GitterRecorder(self.cassette_library_dir, self.git_counter)

    def setup_repo(self, mergify_config, test_branches=[], files=[]):
        self.git("init")
        self.git.configure()
        self.git.add_cred(config.MAIN_TOKEN, "", self.r_o_integration.full_name)
        self.git.add_cred(
            config.FORK_TOKEN,
            "",
            "%s/%s" % (self.u_fork.login, self.r_o_integration.name),
        )
        self.git("config", "user.name", "%s-tester" % config.CONTEXT)
        self.git("remote", "add", "main", self.url_main)
        self.git("remote", "add", "fork", self.url_fork)

        with open(self.git.tmp + "/.mergify.yml", "w") as f:
            f.write(mergify_config)
        self.git("add", ".mergify.yml")

        if files:
            for name, content in files.items():
                with open(self.git.tmp + "/" + name, "w") as f:
                    f.write(content)
                self.git("add", name)

        self.git("commit", "--no-edit", "-m", "initial commit")

        for test_branch in test_branches:
            self.git("branch", test_branch, "master")

        self.git("push", "--quiet", "main", "master", *test_branches)

        self.r_fork = self.u_fork.create_fork(self.r_o_integration)

        for _ in range(10):
            try:
                self.git("fetch", "--quiet", "fork")
            except subprocess.CalledProcessError as e:
                if (
                    b"fatal: remote error: access denied "
                    b"or repository not exported" in e.output
                ):
                    if RECORD:
                        # Forks can take some time, retry
                        time.sleep(0.2)
                else:
                    raise
            else:
                break
        else:
            raise RuntimeError("Unable to fetch from the forked repository")

    @staticmethod
    def response_filter(response):
        for h in [
            "X-GitHub-Request-Id",
            "Date",
            "ETag",
            "X-RateLimit-Reset",
            "Expires",
            "Fastly-Request-ID",
            "X-Timer",
            "X-Served-By",
            "Last-Modified",
            "X-RateLimit-Remaining",
            "X-Runtime-rack",
        ]:
            response["headers"].pop(h, None)
        return response

    def create_pr(
        self,
        base="master",
        files=None,
        two_commits=False,
        base_repo="fork",
        branch=None,
        message=None,
    ):
        self.pr_counter += 1

        if not branch:
            branch = "%s/pr%d" % (base_repo, self.pr_counter)
        title = "Pull request n%d from %s" % (self.pr_counter, base_repo)

        self.git("checkout", "--quiet", "%s/%s" % (base_repo, base), "-b", branch)
        if files:
            for name, content in files.items():
                with open(self.git.tmp + "/" + name, "w") as f:
                    f.write(content)
                self.git("add", name)
        else:
            open(self.git.tmp + "/test%d" % self.pr_counter, "wb").close()
            self.git("add", "test%d" % self.pr_counter)
        self.git("commit", "--no-edit", "-m", title)
        if two_commits:
            self.git("mv", "test%d" % self.pr_counter, "test%d-moved" % self.pr_counter)
            self.git("commit", "--no-edit", "-m", "%s, moved" % title)
        self.git("push", "--quiet", base_repo, branch)

        if base_repo == "fork":
            repo = self.r_fork.parent
            login = self.r_fork.owner.login
        else:
            repo = self.r_o_admin
            login = self.r_o_admin.owner.login

        p = repo.create_pull(
            base=base,
            head="%s:%s" % (login, branch),
            title=title,
            body=message or title,
        )

        self.wait_for("pull_request", {"action": "opened"})

        # NOTE(sileht): We return the same but owned by the main project
        p = self.r_o_integration.get_pull(p.number)
        commits = list(p.get_commits())

        return p, commits

    def create_status(
        self, pr, context="continuous-integration/fake-ci", state="success"
    ):
        # TODO(sileht): monkey patch PR with this
        self.r_o_admin._requester.requestJsonAndCheck(
            "POST",
            pr.base.repo.url + "/statuses/" + pr.head.sha,
            input={
                "state": state,
                "description": "Your change works",
                "context": context,
            },
            headers={"Accept": "application/vnd.github.machine-man-preview+json"},
        )
        self.wait_for("status", {"state": state})

    def create_review(self, pr, commit, event="APPROVE"):
        pr_review = self.r_o_admin.get_pull(pr.number)
        r = pr_review.create_review(commit, "Perfect", event=event)
        self.wait_for("pull_request_review", {"action": "submitted"})
        return r

    def add_label(self, pr, label):
        self.r_o_admin.create_label(label, "000000")
        pr.add_to_labels(label)
        self.wait_for("pull_request", {"action": "labeled"})

    def branch_protection_protect(self, branch, rule):
        if (
            self.r_o_admin.organization
            and rule["protection"]["required_pull_request_reviews"]
        ):
            rule = copy.deepcopy(rule)
            rule["protection"]["required_pull_request_reviews"][
                "dismissal_restrictions"
            ] = {}

        # NOTE(sileht): Not yet part of the API
        # maybe soon https://github.com/PyGithub/PyGithub/pull/527
        return self.r_o_admin._requester.requestJsonAndCheck(
            "PUT",
            "{base_url}/branches/{branch}/protection".format(
                base_url=self.r_o_admin.url, branch=branch
            ),
            input=rule["protection"],
            headers={"Accept": "application/vnd.github.luke-cage-preview+json"},
        )
