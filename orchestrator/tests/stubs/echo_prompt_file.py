import sys

# Mimics a prompt_file-transport CLI (e.g. Grok's --prompt-file <path>):
# reads the prompt from a file path given as argv, not from stdin. Also
# records what (if anything) stdin carried, so tests can prove a
# prompt_file adapter never gets the instruction over stdin.
prompt_file_arg_index = sys.argv.index("--prompt-file") + 1
prompt_path = sys.argv[prompt_file_arg_index]
out_path = sys.argv[sys.argv.index("--out") + 1]

with open(prompt_path, "rb") as fh:
    prompt_body = fh.read()

stdin_body = sys.stdin.buffer.read()

with open(out_path, "wb") as fh:
    fh.write(b"PROMPT_FILE_PATH:" + prompt_path.encode("utf-8") + b"\n")
    fh.write(b"PROMPT_BODY_LEN:" + str(len(prompt_body)).encode("ascii") + b"\n")
    fh.write(b"STDIN_LEN:" + str(len(stdin_body)).encode("ascii") + b"\n")
    fh.write(b"PROMPT_BODY:" + prompt_body)

sys.exit(0)
