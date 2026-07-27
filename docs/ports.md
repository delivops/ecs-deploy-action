# Port Configuration

You can configure the main application port and additional named ports.

## Example YAML

```yaml
port: 8080
additional_ports:
  - metrics: 9090
  - health: 8081
```

The main `port` is always named `default` in the generated task definition; each entry in
`additional_ports` is a single-key mapping of `name: port`, and that key becomes the port mapping's
name. Both are optional — omit them entirely for background workers, as in
[`examples/no-ports.yaml`](../examples/no-ports.yaml).

## `app_protocol`

`app_protocol` sets `appProtocol` on **every** port mapping, main and additional alike. It defaults
to `http`:

```yaml
port: 9090
app_protocol: grpc
```

Setting `app_protocol: tcp` omits the `appProtocol` field altogether, which is what ECS expects for
plain TCP. There is no way to set a different protocol per port.

## Host ports

`hostPort` is derived from the task's network mode, not configured directly:

| Network mode | `hostPort` |
|---|---|
| `awsvpc` (Fargate and EC2), `host` | equals `containerPort` |
| `bridge` (EC2 only) | `0`, for dynamic assignment |

See [EC2 Launch Type](ec2-launch-type.md).

## Sidecar ports

The OpenTelemetry collector sidecar always adds its own mappings for 4317 (gRPC) and 4318 (HTTP);
these are independent of `port` and `additional_ports`. See [OpenTelemetry Collector](otel.md).
