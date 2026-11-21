#!/usr/bin/env python3
"""Ensure the restricted Forgejo updater account can use each repository's automation.

A restricted Forgejo account has no implicit repository access, so its API calls
(find/create the input PR, push the update branch, maintain the monitoring issue)
answer 404 until an explicit collaborator row exists. The installer only writes
files, so the owner runs this once per repository with an admin token.

Write access on only the repository whose workflow uses UPDATE_TOKEN is the least
the updater needs: it pushes a branch and opens PRs and issues there. It reads other
repositories' inputs with a different account. Existing write/admin access is left
alone, read access is raised to write, and nothing is ever removed.

The token comes only from FORGEJO_ADMIN_TOKEN and is never printed.
Use --check to report missing access without changing anything (exit 1 if any).
"""
import argparse
import json
import os
import re
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import HTTPRedirectHandler, Request, build_opener

DEFAULT_HOST = 'https://git.monke.fail'
DEFAULT_USER = 'flakey-updater'
RANK = {'none': 0, 'read': 1, 'write': 2, 'admin': 3, 'owner': 4}
NAME = re.compile(r'[A-Za-z0-9._-]+')


class Failure(Exception):
    """A message that is safe to print."""


def request(host, token, method, path, data=None):
    class NoRedirect(HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            raise Failure('API redirect refused')
    req = Request(host + '/api/v1' + path, method=method,
                  data=None if data is None else json.dumps(data).encode(),
                  headers={'Authorization': 'token ' + token, 'Content-Type': 'application/json',
                           'Accept': 'application/json', 'User-Agent': 'ensure-updater-access'})
    with build_opener(NoRedirect()).open(req, timeout=60) as response:
        body = response.read()
        return json.loads(body) if body else None


def current_permission(host, token, repository, user):
    path = '/repos/%s/collaborators/%s/permission' % (repository, quote(user))
    try:
        return (request(host, token, 'GET', path) or {}).get('permission', 'none')
    except HTTPError as error:
        error.close()
        if error.code == 404:
            return 'none'
        raise


def ensure(host, token, repository, user, permission, check):
    """Return 'ok', 'granted', 'raised' or 'missing' (check mode)."""
    have = current_permission(host, token, repository, user)
    if RANK.get(have, 0) >= RANK[permission]:
        return 'ok'
    if check:
        return 'missing'
    request(host, token, 'PUT', '/repos/%s/collaborators/%s' % (repository, quote(user)),
            {'permission': permission})
    return 'raised' if have != 'none' else 'granted'


def explain(error, repository, user):
    if isinstance(error, HTTPError):
        code = error.code
        error.close()
        hint = {401: 'the admin token was rejected', 403: 'the token needs repository admin rights',
                404: 'the repository or account was not found (check names, and that the token can see the repository)'
                }.get(code, 'unexpected response')
        return '%s: HTTP %d, %s (account %s)' % (repository, code, hint, user)
    if isinstance(error, URLError):
        return '%s: could not reach the forge' % repository
    return '%s: %s' % (repository, error)


def main(argv=None, environ=None):
    environ = os.environ if environ is None else environ
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--repository', action='append', required=True, metavar='OWNER/NAME')
    parser.add_argument('--host', default=DEFAULT_HOST)
    parser.add_argument('--user', default=DEFAULT_USER)
    parser.add_argument('--permission', choices=['read', 'write'], default='write')
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args(argv)
    token = environ.get('FORGEJO_ADMIN_TOKEN', '')
    if not token:
        print('FORGEJO_ADMIN_TOKEN is not set; export an admin-capable token for ' + args.host, file=sys.stderr)
        return 2
    if not args.host.startswith('https://'):
        print('--host must be an https URL', file=sys.stderr)
        return 2
    if not NAME.fullmatch(args.user):
        print('Invalid --user', file=sys.stderr)
        return 2
    failed = False
    for repository in args.repository:
        owner, _, name = repository.partition('/')
        if not (NAME.fullmatch(owner) and NAME.fullmatch(name)):
            print('Invalid repository ' + repr(repository) + ' (expected OWNER/NAME)', file=sys.stderr)
            failed = True
            continue
        try:
            result = ensure(args.host.rstrip('/'), token, repository, args.user, args.permission, args.check)
        except (HTTPError, URLError, Failure) as error:
            print(explain(error, repository, args.user), file=sys.stderr)
            failed = True
            continue
        print('%s: %s %s access %s' % (repository, args.user, args.permission, result))
        failed = failed or result == 'missing'
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
