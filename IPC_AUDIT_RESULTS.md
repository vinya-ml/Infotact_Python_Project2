# ChronosMatch — Mid-Project Review: IPC Audit Results

## Requirement (from project spec)

> IPC Audit: Prove the zero-copy architecture works by sending 1M orders
> between two Python processes without hitting the CPU bottleneck of Pickling.

## Method

- `src/data_processing/ipc_buffer.py` — a memory-mapped (`mmap`) ring buffer
  shared between two independent OS processes, using a single-writer /
  single-reader header design (each process owns a separate index field,
  eliminating the read-modify-write race that a shared header would cause).
- `ipc_proof.py` — spawns a real writer process and a real reader process
  via Python's `multiprocessing` module (not a simulation within one
  process), both pointing at the same backing file. Orders are created and
  serialized with `src/data_processing/processor.py`'s existing 33-byte
  binary format (`<QBdQQ`).
- A pickle-based baseline was run for comparison, serializing/deserializing
  the same number of orders in a single process, representing the
  traditional non-zero-copy approach the project spec says is too slow.

## Results

| Orders | mmap IPC time | mmap throughput | Pickle time | Pickle throughput | Speedup |
|---|---|---|---|---|---|
| 10,000 | 0.028 sec | 353,384 orders/sec | 0.069 sec | 144,921 orders/sec | 2.4x |
| 1,000,000 | 3.086 sec | 324,044 orders/sec | 6.862 sec | 145,738 orders/sec | 2.2x |

## Conclusion

The zero-copy `mmap` architecture successfully moved 1,000,000 orders
between two separate Python processes in 3.086 seconds (~324K orders/sec),
consistently outperforming the pickle-based baseline by more than 2x at
both small and full scale. No serialization bottleneck was observed at
the 1M-order target specified in the Mid-Project Review requirement.

## How to reproduce

From the project root:

```bash
python ipc_proof.py --orders 1000000
```

## Notes / Honest Limitations

- The ring buffer is sized to hold the entire test run at once
  (`num_slots = num_orders`) to avoid wraparound during this specific proof.
  A production version would need backpressure (the writer waiting when
  the buffer is full) to handle sustained/unbounded order flow safely.
- This design supports exactly one writer and one reader process at a
  time. Multiple concurrent writers or readers would require proper
  locking or atomic operations, which this proof does not implement.