"""`python -m isoplay` and the frozen build both land here."""
import sys


def run():
    # The updater helper must run before Qt/libmpv load DLLs from the installation being replaced.
    if len(sys.argv) == 3 and sys.argv[1] == "--apply-update":
        from isoplay.updates import apply_update
        raise SystemExit(apply_update(sys.argv[2]))
    from isoplay.app import main
    main()


if __name__ == "__main__":
    run()
