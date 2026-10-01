"""Trusted server-operator ownership assignment; not an HTTP capability."""
import argparse
import json
from collaboration_store import Store
from controller import DEFAULT_STATE


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--state',default=str(DEFAULT_STATE))
    sub=parser.add_subparsers(dest='action',required=True)
    bind=sub.add_parser('bind-node')
    bind.add_argument('--username',required=True)
    bind.add_argument('--node',required=True)
    args=parser.parse_args()
    store=Store(args.state)
    with store.connect() as db:
        user=db.execute('SELECT id FROM users WHERE username=?',(args.username,)).fetchone()
        if not user: parser.error('Create the invited human account first')
    store.bind_node(user['id'],args.node)
    print(json.dumps({'node':args.node,'owner':args.username}))


if __name__=='__main__': main()
