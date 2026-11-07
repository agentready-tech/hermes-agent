"""Native executable stand-in: process/profile/listener proof without Chrome or a GUI."""
import contextlib
import shutil
import subprocess
import sys

import pytest

from tests.waiting import wait_until

_SOURCE = r'''
#include <arpa/inet.h>
#include <errno.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <unistd.h>
int main(int argc, char **argv) {
  const char *profile = argv[1] + strlen("--user-data-dir=");
  char path[4096], host[256], target[300];
  gethostname(host, sizeof(host));
  snprintf(target, sizeof(target), "%s-%d", host, getpid());
  snprintf(path, sizeof(path), "%s/SingletonLock", profile);
  if (symlink(target, path)) {
    char previous[300]; ssize_t length = readlink(path, previous, sizeof(previous)-1);
    if (length < 0) return 2;
    previous[length] = 0; char *pid = strrchr(previous, '-');
    if (!pid || kill(atoi(pid+1), 0) == 0 || errno != ESRCH) return 2;
    if (unlink(path) || symlink(target, path)) return 2;
  }
  if (argc == 2 || strcmp(argv[2], "--remote-debugging-pipe")) {
    int fd = socket(AF_INET, SOCK_STREAM, 0);
    struct sockaddr_in address = {.sin_family=AF_INET, .sin_port=0};
    address.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    if (bind(fd, (struct sockaddr *)&address, sizeof(address)) || listen(fd, 16)) return 3;
    socklen_t length = sizeof(address);
    getsockname(fd, (struct sockaddr *)&address, &length);
    snprintf(path, sizeof(path), "%s/DevToolsActivePort", profile);
    FILE *out = fopen(path, "w");
    fprintf(out, "%d\n/devtools/browser/fixture\n", ntohs(address.sin_port)); fclose(out);
  }
  snprintf(path, sizeof(path), "%s/fixture-ready", profile);
  FILE *ready = fopen(path, "w"); fprintf(ready, "%d", getpid()); fclose(ready);
  char value; read(STDIN_FILENO, &value, 1);
  return 0;
}
'''


@pytest.fixture(scope='module')
def native_chrome_binary(tmp_path_factory):
    compiler = shutil.which('cc')
    if compiler is None:
        pytest.skip('native process fixture needs a C compiler')
    binary = tmp_path_factory.mktemp('browser-native') / 'chrome'
    subprocess.run([compiler, '-x', 'c', '-', '-o', str(binary)], input=_SOURCE.encode(),
                   capture_output=True, check=True, timeout=30)
    return binary


@pytest.fixture
def native_chrome(native_chrome_binary, monkeypatch):
    from tools.bot_desktop import browser_instance
    active = set()
    monkeypatch.setattr(browser_instance, '_candidates', lambda: iter(active))
    @contextlib.contextmanager
    def running(profile, *, pipe=False, session=None):
        profile.mkdir(parents=True, exist_ok=True)
        (profile / 'fixture-ready').unlink(missing_ok=True)
        command = [str(native_chrome_binary), '--user-data-dir=' + str(profile)]
        if pipe:
            command.append('--remote-debugging-pipe')
        options = {}
        if session:
            import os
            options['env'] = {**os.environ, 'AGENT_BROWSER_SESSION': session}
            code = ('import subprocess,sys; p=subprocess.Popen(' + repr(command) +
                    ',stdin=subprocess.PIPE);sys.stdin.buffer.read(1);p.stdin.close();p.wait(timeout=5)')
            command = [sys.executable, '-c', code]
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, **options)
        pid = None
        try:
            wait_until(lambda: (profile / 'fixture-ready').exists() or process.poll() is not None,
                       what='native browser fixture ready')
            assert process.poll() is None
            pid = int((profile / 'fixture-ready').read_text())
            active.add(pid)
            yield pid
        finally:
            active.discard(pid)
            process.stdin.close()
            process.wait(timeout=5)
    return running
