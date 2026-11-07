"""List a WebDAV share, so a disc folder on one can be projected like any other.

Only PROPFIND is used, one `Depth: 1` level at a time: `Depth: infinity` is refused
by most servers. Nothing here knows how to make a request; the caller passes that in.
"""
import urllib.parse
from xml.etree import ElementTree

DAV = "{DAV:}"
PROPS = (b'<?xml version="1.0" encoding="utf-8"?>'
         b'<propfind xmlns="DAV:"><prop><resourcetype/><getcontentlength/></prop></propfind>')
MAX_FILES = 20000  # a Blu-ray is a few hundred; this is only here to stop a walk running away
MAX_DEPTH = 6


def children(body, base):
    """(url, size) for each entry of a Multi-Status body; size is None for a collection."""
    try:
        tree = ElementTree.fromstring(body)
    except Exception:
        raise OSError("That link isn't a WebDAV share") from None
    out = []
    for response in tree.findall(DAV + "response"):
        href = response.findtext(DAV + "href")
        prop = response.find(f"{DAV}propstat/{DAV}prop")
        if href is None or prop is None:
            continue
        collection = prop.find(f"{DAV}resourcetype/{DAV}collection") is not None
        length = prop.findtext(DAV + "getcontentlength")
        out.append((urllib.parse.urljoin(base, href.strip()),
                    None if collection else int(length) if length and length.isdigit() else 0))
    return out


def walk(root, propfind):
    """{path below root: (url, size)} for every file on the share."""
    root = root if root.endswith("/") else root + "/"
    base = urllib.parse.urlsplit(root).path
    files, todo = {}, [root]
    while todo:
        url = todo.pop()
        listed = children(propfind(url), url)
        if url == root and not listed:
            raise OSError("That link isn't a WebDAV share")
        for child, size in listed:
            path = urllib.parse.urlsplit(child).path
            if path.rstrip("/") == urllib.parse.urlsplit(url).path.rstrip("/"):
                continue  # a collection lists itself first
            if not path.startswith(base):
                continue
            if size is None:
                if child.count("/") - root.count("/") < MAX_DEPTH:
                    todo.append(child if child.endswith("/") else child + "/")
                continue
            files[urllib.parse.unquote(path[len(base):])] = (child, size)
            if len(files) > MAX_FILES:
                raise OSError("That share has too many files to open as a disc")
    return files
