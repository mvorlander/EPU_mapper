"""Compatibility entry point; density is now part of the main acquisition app."""
import argparse
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'src'));sys.path.insert(0,str(ROOT))
from acquisition_app import create_acquisition_app


def create_density_app(source, atlas=None, cache_root=None):
    return create_acquisition_app(source,atlas,mode='acquisition',cache_root=cache_root)


def main():
    parser=argparse.ArgumentParser(description='EPU Mapper acquisition and particle density')
    parser.add_argument('source',nargs='?',type=Path)
    parser.add_argument('--atlas',type=Path)
    parser.add_argument('--port',type=int,default=0)
    parser.add_argument('--no-browser',action='store_true')
    args=parser.parse_args()
    if not args.source:
        from launcher import launch
        launch(atlas=args.atlas)
        return
    if not args.source.is_dir():parser.error('Session folder unavailable; reconnect the share.')
    if args.atlas and not args.atlas.exists():parser.error('Atlas path unavailable; reconnect the share.')
    from server_startup import reserve_socket,announce_address,run_reserved_server
    app=create_density_app(args.source,args.atlas)
    with reserve_socket('127.0.0.1',args.port,True) as listener:
        address=announce_address('127.0.0.1',args.port,listener)
        run_reserved_server(app,listener,address,open_browser=not args.no_browser)


if __name__=='__main__':main()
