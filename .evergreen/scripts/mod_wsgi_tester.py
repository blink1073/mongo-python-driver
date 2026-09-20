from __future__ import annotations

import os
import socket
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from shutil import which

from utils import LOGGER, ROOT, run_command, write_env


def make_request(url, timeout=10):
    for _ in range(int(timeout)):
        try:
            urllib.request.urlopen(url)  # noqa: S310
            return
        except urllib.error.HTTPError:
            pass
        time.sleep(1)
    raise TimeoutError(f"Failed to access {url}")


def find_mod_wsgi_so() -> str:
    # pip builds the Apache module against the installing interpreter, so the
    # .so lives inside the installed package.
    import mod_wsgi

    so_files = list((Path(mod_wsgi.__file__).parent / "server").glob("mod_wsgi*.so"))
    if len(so_files) != 1:
        raise ValueError(f"Expected one mod_wsgi.so in {mod_wsgi.__file__}, found {so_files}")
    return str(so_files[0])


def find_apache() -> tuple[str, str]:
    """Return the Apache binary and the config file matching its distribution."""
    apache = which("apache2")
    if not apache:
        # apache2 lives in sbin, which is not always on PATH for non-root users.
        for candidate in ("/usr/lib/apache2/mpm-prefork/apache2", "/usr/sbin/apache2"):
            if Path(candidate).exists():
                apache = candidate
                break
    if apache:
        return apache, "apache24ubuntu.conf"
    apache = which("httpd")
    if not apache:
        for candidate in ("/usr/sbin/httpd", "/usr/local/apache2/bin/httpd"):
            if Path(candidate).exists():
                apache = candidate
                break
    if not apache:
        raise ValueError("Could not find apache2 or httpd")
    return apache, "httpd24fedora.conf"


def setup_mod_wsgi(sub_test_name: str) -> None:
    env = os.environ.copy()
    if sub_test_name == "embedded":
        env["MOD_WSGI_CONF"] = "mod_wsgi_test_embedded.conf"
    elif sub_test_name == "standalone":
        env["MOD_WSGI_CONF"] = "mod_wsgi_test.conf"
    else:
        raise ValueError("mod_wsgi sub test must be either 'standalone' or 'embedded'")
    write_env("MOD_WSGI_CONF", env["MOD_WSGI_CONF"])
    apache, apache_config = find_apache()
    so_file = find_mod_wsgi_so()
    write_env("MOD_WSGI_SO", so_file)
    env["MOD_WSGI_SO"] = so_file
    # On Fedora/RHEL, httpd embeds the interpreter mod_wsgi was built with and
    # needs to be told where the Python installation lives.
    python_home = sys.base_prefix
    write_env("MOD_WSGI_PYTHON_HOME", python_home)
    env["MOD_WSGI_PYTHON_HOME"] = python_home
    env["PROJECT_DIRECTORY"] = project_directory = str(ROOT)
    write_env("PROJECT_DIRECTORY", project_directory)
    write_env("APACHE_BINARY", apache)
    write_env("APACHE_CONFIG", apache_config)
    uri1 = f"http://localhost:8080/interpreter1{project_directory}"
    write_env("TEST_URI1", uri1)
    uri2 = f"http://localhost:8080/interpreter2{project_directory}"
    write_env("TEST_URI2", uri2)
    # Stop any server left behind by a previous failed run so setup is
    # idempotent and the port is free.
    stop_mod_wsgi(apache, apache_config)
    run_command(f"{apache} -k start -f {ROOT}/test/mod_wsgi_test/{apache_config}", env=env)

    # Wait for the endpoints to be available.
    try:
        make_request(uri1, 10)
        make_request(uri2, 10)
    except Exception as e:
        LOGGER.error(Path("error_log").read_text())
        raise e


def test_mod_wsgi() -> None:
    sys.path.insert(0, ROOT)
    from test.mod_wsgi_test.test_client import main, parse_args

    uri1 = os.environ["TEST_URI1"]
    uri2 = os.environ["TEST_URI2"]
    args = f"-n 25000 -t 100 parallel {uri1} {uri2}"
    try:
        main(*parse_args(args.split()))

        args = f"-n 25000 serial {uri1} {uri2}"
        main(*parse_args(args.split()))
    except BaseException:
        # The test client raises KeyboardInterrupt in the failing path.
        LOGGER.error(Path("error_log").read_text())
        raise


def stop_mod_wsgi(apache: str, apache_config: str) -> None:
    # Idempotent: safe to call when Apache is not running, so teardown can be
    # unconditional even if setup or the test failed partway through.
    run_command(
        f"{apache} -k stop -f {ROOT}/test/mod_wsgi_test/{apache_config}",
        check=False,
    )

    # -k stop signals the master process and returns before it exits. Wait for
    # the port to be released so another instance can bind it.
    deadline = time.time() + 10
    while time.time() < deadline:
        try:
            socket.create_connection(("127.0.0.1", 8080), timeout=1).close()
        except OSError:
            return
        time.sleep(0.5)
    raise ValueError("Apache did not release port 8080")


def teardown_mod_wsgi() -> None:
    apache = os.environ["APACHE_BINARY"]
    apache_config = os.environ["APACHE_CONFIG"]

    stop_mod_wsgi(apache, apache_config)


if __name__ == "__main__":
    setup_mod_wsgi()
