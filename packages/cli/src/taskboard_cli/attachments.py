"""Transfer file bytes through the signed Taskboard attachment actions."""

from base64 import b64decode, b64encode
import json
from mimetypes import guess_type
import os
from pathlib import Path
from tempfile import NamedTemporaryFile


READS = {"attachment-list", "attachment-download"}
WRITES = {"attachment-upload", "attachment-remove"}


def transfer(board, args):
    params = {"id": args.ticket}
    if args.command == "attachment-list":
        return json.dumps(board.invoke("list-attachments", params), ensure_ascii=False)
    if args.command == "attachment-upload":
        limits = board.invoke("list-attachments", params)
        if not limits["can_edit"]:
            raise ValueError("This ticket requires project write access.")
        limit = limits["max_upload_bytes"]
        if type(limit) is not int or not 0 < limit <= 1024 * 1024:
            raise ValueError("The board did not report a valid attachment upload limit.")
        path = Path(args.file)
        with path.open("rb") as source:
            contents = source.read(limit + 1)
        if len(contents) > limit:
            raise ValueError(f"The attachment exceeds the signed upload limit of {limit} bytes.")
        result = board.invoke("upload-attachment", {
            **params, "upload_id": args.upload_id, "filename": path.name,
            "content_type": guess_type(path.name)[0] or "application/octet-stream",
            "content_base64": b64encode(contents).decode("ascii"),
        }, write=True)
        return json.dumps(result["attachment"], ensure_ascii=False)

    params["attachment_id"] = args.attachment_id
    if args.command == "attachment-remove":
        return board.invoke("remove-attachment", params, write=True)["receipt"]

    output = Path(args.output)
    if output.exists():
        raise FileExistsError(f"Output already exists: {output}")
    offset = 0
    size = None
    with NamedTemporaryFile(dir=output.parent, prefix=".attachment-", delete=False) as handle:
        staged = Path(handle.name)
        try:
            while True:
                chunk = board.invoke("read-attachment-chunk", {**params, "offset": offset})
                contents = b64decode(chunk["content_base64"], validate=True)
                attachment = chunk["attachment"]
                reported = attachment["size_bytes"]
                if type(reported) is not int or not 0 <= reported <= 1024 * 1024:
                    raise ValueError("The board did not report a valid attachment size.")
                if size is None:
                    size = reported
                end = offset + len(contents)
                following = chunk["next_offset"]
                if (chunk["offset"] != offset or attachment["id"] != args.attachment_id
                        or reported != size or end > size
                        or (end == size and following is not None)
                        or (end < size and (not contents or following != end))):
                    raise ValueError("The board did not return the expected attachment chunk.")
                handle.write(contents)
                if following is None:
                    break
                offset = following
            handle.close()
            os.link(staged, output)
        finally:
            staged.unlink(missing_ok=True)
    return f"Downloaded {size} bytes to {output}."
