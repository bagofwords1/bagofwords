# sandboxd protocol stubs

`process.proto` is copied unchanged from
[kubernetes-sigs/agent-sandbox](https://github.com/kubernetes-sigs/agent-sandbox/blob/b0040f5e51a4b98341af899e3ce9da767e63216f/packages/sandboxd/spec/process/v1/process.proto)
at the pinned commit `b0040f5e51a4b98341af899e3ce9da767e63216f` (Apache-2.0),
the same commit the `sandboxd` binary is built from.

`process_pb2.py` and `process_pb2_grpc.py` are generated. Do not edit them.
The generator must not be newer than the backend's `protobuf` runtime (5.x),
so they are produced with `grpcio-tools==1.71.0`:

```bash
cd backend
uv venv /tmp/gen && uv pip install --python /tmp/gen/bin/python "grpcio-tools==1.71.0"
/tmp/gen/bin/python -m grpc_tools.protoc -I. --python_out=. --grpc_python_out=. \
    app/services/sandbox/sandboxd_proto/process.proto
```

The file API is plain REST (`spec/filesystem/v1/filesystem.yaml` upstream) and
needs no generated code.

Only `runtime_client.py` and the test stand-in `testing/sandboxd_stub.py`
import these modules, and they do so lazily.
