#!/usr/bin/env python3
"""
ABM: Active Buffer Management in Datacenters - Figure 6 Replication
CIS 437/537 Final Project - Fnu Nidhi (UMID: 92553817)

Paper: Addanki et al., "ABM: Active Buffer Management in Datacenters"
       ACM SIGCOMM 2022, https://dl.acm.org/doi/10.1145/3544216.3544252

SIMULATOR: Event-driven packet-level simulator with Leaf-Spine topology.

Topology (simplified leaf-spine, matching paper Section 4.1):
  ┌──────────┐       ┌──────────┐
  │  Spine0  │       │  Spine1  │
  └──┬───┬───┘       └───┬───┬──┘
     │   │               │   │
  ┌──┴───┴───┐       ┌───┴───┴──┐
  │  Leaf0   │       │  Leaf1   │    ← Shared buffer + BM here
  └┬─┬─┬─┬──┘       └──┬─┬─┬─┬┘
   │ │ │ │              │ │ │ │
  H0 H1 H2 H3        H4 H5 H6 H7   ← 8 hosts total

  - All links: 10 Gbps, 10 μs propagation delay
  - Leaf switches: shared buffer with DT (Dynamic Thresholds),
    CS (Complete Sharing), and ABM (Active Buffer Management)
  - Spine switches: forwarding only (infinite buffer)
  - Hosts generate web-search and incast traffic

Key equations from the paper:
  DT  (Eq.5):  T = α · (B − Q(t))
  ABM (Eq.9):  T = α · (1/n_p) · (B − Q(t)) · (μ_pi / b)
"""

import numpy as np
import heapq
import sys
import os
import json

_PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))

# ============================================================
# Network Parameters (Section 4.1)
# ============================================================
LINK_BW      = 10e9          # 10 Gbps
LINK_DELAY   = 10e-6         # 10 μs propagation delay per link
PKT_SIZE     = 1000          # 1000 bytes
PKT_BITS     = PKT_SIZE * 8
TX_TIME      = PKT_BITS / LINK_BW   # ~0.8 μs per packet
MIN_RTO      = 10e-3         # 10 ms retransmit timeout (paper Sec 4.1)

# Topology
HOSTS_PER_LEAF = 8
NUM_LEAVES     = 2
NUM_SPINES     = 2
TOTAL_HOSTS    = HOSTS_PER_LEAF * NUM_LEAVES  # 16

# Path delays (host → leaf → spine → leaf → host)
LEAF_SPINE_DELAY = LINK_DELAY             # 10 μs
HOST_LEAF_DELAY  = LINK_DELAY             # 10 μs
ONE_WAY_DELAY    = 2 * LINK_DELAY         # host→leaf→spine = 20 μs
RTT_BASE         = 4 * LINK_DELAY         # full RTT = 40 μs (no queuing)

# Buffer (shared per leaf switch)
SHARED_BUF    = 40           # shared buffer — fills under DT but not ABM
NUM_PORTS     = HOSTS_PER_LEAF  # 4 egress ports per leaf
ALPHA         = 0.5
ALPHA_U       = 64           # alpha for unscheduled (first-RTT) packets

# ============================================================
# Web-search flow size distribution (Data Center TCP (DCTCP), SIGCOMM 2010)
# ============================================================
_CDF = [(6e3,.15),(13e3,.2),(19e3,.3),(33e3,.4),(53e3,.53),
        (133e3,.6),(667e3,.7),(1.33e6,.8),(3.33e6,.9),(6.67e6,.97),(20e6,1.)]

def sample_flow_size(rng):
    r = rng.random()
    ps, pp = 1e3, 0.
    for s, p in _CDF:
        if r <= p:
            return max(PKT_SIZE, int(ps + (r - pp) / (p - pp) * (s - ps)))
        ps, pp = s, p
    return int(20e6)

def ideal_fct(size_bytes):
    """Ideal flow completion time (FCT) = round-trip time (RTT) + serialization delay."""
    return RTT_BASE + (size_bytes * 8) / LINK_BW

# ============================================================
# Buffer Management Schemes at Leaf Switch
# ============================================================

class SharedBuffer:
    """Shared buffer pool at a leaf switch with multiple egress ports."""
    def __init__(self, total_buf, num_ports, scheme='DT'):
        self.B = total_buf
        self.num_ports = num_ports
        self.scheme = scheme
        # Per-port queue lengths
        self.queues = np.zeros(num_ports)
        # For ABM (Active Buffer Management): drain-rate tracking
        self.drain_rates = np.ones(num_ports) * LINK_BW
        self._deq_counts = np.zeros(num_ports)
        self._last_update = 0.0
        # Stats
        self.total_enqueued = 0
        self.total_dropped = 0

    def total_occ(self):
        return self.queues.sum()

    def occ_pct(self):
        return self.total_occ() / self.B * 100

    def _n_congested(self):
        """Count queues that are congested (ABM: Section 3.1)."""
        fair_share = self.B * ALPHA / self.num_ports
        return max(1, int((self.queues > max(1, 0.9 * fair_share)).sum()))

    def _drain_norm(self, port):
        """Normalized drain rate μ/b for ABM (Active Buffer Management)."""
        return np.clip(self.drain_rates[port] / LINK_BW, 0.01, 1.0)

    def threshold(self, port, is_unscheduled=False):
        """Compute admission threshold for a port's queue."""
        remaining = max(0, self.B - self.total_occ())

        if self.scheme == 'DT':
            # Equation 5: T = α(B - Q)
            return ALPHA * remaining

        elif self.scheme == 'CS':
            # Complete Sharing: any queue can use all remaining buffer
            return remaining

        elif self.scheme == 'ABM':
            # Equation 9: T = α · (1/n_p) · (B-Q) · (μ/b)
            # ABM shrinks admission as more ports become congested and
            # raises it for ports that are draining quickly.
            n_p = self._n_congested()
            mu = self._drain_norm(port)
            alpha = ALPHA_U if is_unscheduled else ALPHA
            return alpha * (1.0 / n_p) * remaining * mu

    def try_enqueue(self, port, is_unscheduled=False):
        """
        Try to admit a packet. Returns (admitted, ecn_marked).
        
        ABM uses Explicit Congestion Notification (ECN) marking (not hard drops)
        for scheduled traffic:
        - Unscheduled (incast first round-trip time (RTT)): hard drop if queue > threshold
        - Scheduled (web-search): admit but ECN-mark if queue > threshold
          Hard drop only if total buffer is physically full
        DT (Dynamic Thresholds) / CS (Complete Sharing): always hard drop if queue > threshold
        """
        thr = self.threshold(port, is_unscheduled)
        
        if self.scheme == 'ABM' and not is_unscheduled:
            # ABM scheduled: ECN marking below threshold, hard drop when buffer critically full
            if self.total_occ() >= self.B * 0.85:
                # Buffer critically full — hard drop to prevent overflow
                self.total_dropped += 1
                return False, False
            elif self.total_occ() >= self.B:
                self.total_dropped += 1
                return False, False
            else:
                # Scheduled packets stay in the network, but carry an ECN signal
                # once the per-port queue exceeds the ABM threshold.
                ecn = self.queues[port] >= thr
                self.queues[port] += 1
                self.total_enqueued += 1
                return True, ecn
        else:
            # DT, CS, or ABM unscheduled: hard threshold
            if self.queues[port] < thr:
                self.queues[port] += 1
                self.total_enqueued += 1
                return True, False
            else:
                self.total_dropped += 1
                return False, False

    def dequeue(self, port):
        """Remove a packet from queue (transmitted)."""
        if self.queues[port] > 0:
            self.queues[port] -= 1
            self._deq_counts[port] += 1
            return True
        return False

    def update_drain_rates(self, current_time):
        """Update drain rates periodically (ABM: once per round-trip time (RTT))."""
        if self.scheme != 'ABM':
            return
        dt = current_time - self._last_update
        if dt >= RTT_BASE and dt > 0:
            # Use an exponentially weighted moving average (EWMA) so the ABM threshold reacts to sustained port speed
            # rather than single-packet bursts.
            for p in range(self.num_ports):
                rate = self._deq_counts[p] * PKT_BITS / dt
                self.drain_rates[p] = 0.5 * self.drain_rates[p] + 0.5 * rate
                self.drain_rates[p] = np.clip(self.drain_rates[p], 1.0, LINK_BW)
            self._deq_counts[:] = 0
            self._last_update = current_time

# ============================================================
# Leaf-Spine Topology
# ============================================================

class Topology:
    """
    Leaf-Spine datacenter topology.
    Hosts connect to leaf switches; leaf switches connect to spines.
    """
    def __init__(self):
        self.host_to_leaf = {}   # host_id → leaf_id
        self.leaf_to_hosts = {}  # leaf_id → [host_ids]

        # Build topology
        for leaf in range(NUM_LEAVES):
            self.leaf_to_hosts[leaf] = []
            for h in range(HOSTS_PER_LEAF):
                host_id = leaf * HOSTS_PER_LEAF + h
                self.host_to_leaf[host_id] = leaf
                self.leaf_to_hosts[leaf].append(host_id)

    def get_leaf(self, host_id):
        return self.host_to_leaf[host_id]

    def get_egress_port(self, dst_host):
        """Get the egress port on the destination leaf for this host."""
        return dst_host % HOSTS_PER_LEAF

    def get_path_delay(self, src, dst):
        """
        Compute one-way propagation delay through the topology.
        Same leaf:  host→leaf→host = 2 × LINK_DELAY
        Diff leaf:  host→leaf→spine→leaf→host = 4 × LINK_DELAY
        """
        src_leaf = self.get_leaf(src)
        dst_leaf = self.get_leaf(dst)
        if src_leaf == dst_leaf:
            return 2 * LINK_DELAY
        else:
            return 4 * LINK_DELAY  # Cross-rack through spine

    def get_rtt(self, src, dst):
        return 2 * self.get_path_delay(src, dst)

    def print_topology(self):
        print("  Topology: Leaf-Spine Datacenter")
        print(f"    {NUM_SPINES} Spines, {NUM_LEAVES} Leaves, "
              f"{HOSTS_PER_LEAF} hosts/leaf, {TOTAL_HOSTS} hosts total")
        print(f"    Links: {LINK_BW/1e9:.0f} Gbps, {LINK_DELAY*1e6:.0f} us delay")
        print(f"    Buffer: {SHARED_BUF} pkts shared per leaf switch")
        for leaf in range(NUM_LEAVES):
            hosts = self.leaf_to_hosts[leaf]
            print(f"    Leaf{leaf}: hosts {hosts}")

# ============================================================
# Event-Driven Packet Simulator
# ============================================================

class Simulator:
    """
    Event-driven simulator. Packets travel through the topology,
    queue at leaf switch egress ports, and get admitted/dropped
    by the buffer management scheme.
    """
    def __init__(self, topo, scheme='DT', duration=0.04):
        self.topo = topo
        self.scheme = scheme
        self.duration = duration
        self.time = 0.0

        # One shared buffer per leaf switch
        self.leaf_buffers = {}
        for leaf in range(NUM_LEAVES):
            self.leaf_buffers[leaf] = SharedBuffer(SHARED_BUF, HOSTS_PER_LEAF, scheme)

        # Event heap: (time, seq, event_type, data)
        self._heap = []
        self._seq = 0

        # Port scheduling: next free time per (leaf, port)
        self._port_free = {}
        for leaf in range(NUM_LEAVES):
            for port in range(HOSTS_PER_LEAF):
                self._port_free[(leaf, port)] = 0.0

        # Flow tracking
        self.flows = {}
        self.completed_flows = []
        self._fid = 0

        # Buffer occupancy samples
        self.occ_samples = {leaf: [] for leaf in range(NUM_LEAVES)}
        self.total_bytes_out = 0

    def _push(self, t, etype, data):
        self._seq += 1
        heapq.heappush(self._heap, (t, self._seq, etype, data))

    # ---- Flow Management ----

    def _new_flow(self, src, dst, size, start_time, is_incast=False):
        self._fid += 1
        npkts = max(1, min(size // PKT_SIZE, 500))
        f = {
            'id': self._fid, 'src': src, 'dst': dst,
            'size': size, 'npkts': npkts,
            'start': start_time,
            'sent': 0, 'acked': 0, 'dropped': 0,
            # Incast starts more aggressively so the first round-trip time (RTT) behaves like a burst.
            'cwnd': 2 if not is_incast else min(npkts, 10),
            'ssthresh': 64,
            'rtt': self.topo.get_rtt(src, dst),
            'is_incast': is_incast,
            'active': True, 'rto_pending': False,
            'dst_leaf': self.topo.get_leaf(dst),
            'dst_port': self.topo.get_egress_port(dst),
        }
        self.flows[f['id']] = f
        return f

    def _send_packets(self, f):
        """Send as many packets as the congestion window allows."""
        if not f['active']:
            return
        in_flight = f['sent'] - f['acked']
        can_send = min(int(f['cwnd']) - in_flight, f['npkts'] - f['sent'])

        for _ in range(max(0, can_send)):
            # Packet travels: src_host → src_leaf → spine → dst_leaf
            # Queuing happens at dst_leaf's egress port
            arrival_at_switch = self.time + self.topo.get_path_delay(f['src'], f['dst'])

            f['sent'] += 1
            # The first burst of an incast flow is treated as unscheduled traffic,
            # which ABM handles with a much larger alpha_u threshold.
            is_unsched = f['is_incast'] and f['sent'] <= f['cwnd']

            self._push(arrival_at_switch, 'PKT_ARRIVE', {
                'fid': f['id'],
                'leaf': f['dst_leaf'],
                'port': f['dst_port'],
                'is_unsched': is_unsched,
                'pkt_num': f['sent'],
            })

    # ---- Traffic Generation ----

    def generate_websearch(self, load_frac, rng):
        """Generate Poisson web-search traffic between random host pairs."""
        avg_size = 200000
        total_bw = LINK_BW * NUM_LEAVES * HOSTS_PER_LEAF
        rate = load_frac * total_bw / (avg_size * 8)
        if rate <= 0:
            return

        t = 0.0
        while t < self.duration:
            t += rng.exponential(1.0 / rate)
            if t >= self.duration:
                break

            # Random src→dst across different racks (cross-rack traffic)
            src = rng.randint(0, HOSTS_PER_LEAF)  # Leaf0 hosts
            dst = rng.randint(HOSTS_PER_LEAF, TOTAL_HOSTS)  # Leaf1 hosts
            size = sample_flow_size(rng)

            self._push(t, 'FLOW_START', {
                'src': src, 'dst': dst, 'size': size, 'is_incast': False
            })

    def generate_incast(self, request_pct, load_frac, rng):
        """
        Generate incast traffic: multiple hosts simultaneously send
        to one host, simulating distributed query-response pattern.
        """
        req_bytes = int(request_pct * SHARED_BUF * PKT_SIZE)
        n_senders = HOSTS_PER_LEAF  # ALL hosts on Leaf0 send simultaneously (8 senders)
        per_sender = max(PKT_SIZE, req_bytes // n_senders)

        # Incast frequency: sparse enough for buffer to reach steady state
        interval = max(5e-4, 2e-3 / (1 + load_frac * 2))

        t = 0.0
        while t < self.duration:
            t += rng.exponential(interval)
            if t >= self.duration:
                break

            # Incast target: random host on Leaf1
            dst = rng.randint(HOSTS_PER_LEAF, TOTAL_HOSTS)

            # All senders on Leaf0 send simultaneously
            for s in range(n_senders):
                src = s  # Hosts 0,1,2,3 on Leaf0
                self._push(t + s * 1e-9, 'FLOW_START', {
                    'src': src, 'dst': dst, 'size': per_sender, 'is_incast': True
                })

    # ---- Event Handlers ----

    def _handle_flow_start(self, data):
        f = self._new_flow(data['src'], data['dst'], data['size'],
                           self.time, data['is_incast'])
        self._send_packets(f)

    def _handle_pkt_arrive(self, data):
        """Packet arrives at destination leaf switch's egress port."""
        fid = data['fid']
        leaf = data['leaf']
        port = data['port']
        buf = self.leaf_buffers[leaf]

        # Try to admit packet into the shared buffer
        admitted, ecn_marked = buf.try_enqueue(port, data['is_unsched'])

        if fid not in self.flows:
            return

        f = self.flows[fid]

        if admitted:
            # Each egress port is a single-server queue. Once admitted, packets
            # serialize at line rate behind any packet already scheduled there.
            svc_start = max(self.time, self._port_free[(leaf, port)])
            svc_end = svc_start + TX_TIME
            self._port_free[(leaf, port)] = svc_end

            self._push(svc_end, 'PKT_DEPART', {
                'fid': fid, 'leaf': leaf, 'port': port,
                'ecn': ecn_marked,
            })
        else:
            # DROPPED! This triggers a retransmission timeout (RTO) for the flow.
            f['dropped'] += 1
            f['ssthresh'] = max(2, f['cwnd'] // 2)
            f['cwnd'] = max(1, f['cwnd'] // 2)

            if not f['rto_pending']:
                f['rto_pending'] = True
                self._push(self.time + MIN_RTO, 'RTO', fid)

    def _handle_pkt_depart(self, data):
        """Packet transmitted from egress port → travels to destination host."""
        leaf = data['leaf']
        port = data['port']
        buf = self.leaf_buffers[leaf]

        buf.dequeue(port)
        buf.update_drain_rates(self.time)
        self.total_bytes_out += PKT_SIZE

        fid = data['fid']
        if fid not in self.flows:
            return

        f = self.flows[fid]

        # ACK arrives back at sender
        # This adds a small queue-dependent term so deeper queues feed back into
        # the sender's timing even in this simplified transport model.
        ack_delay = HOST_LEAF_DELAY + self.topo.get_path_delay(f['dst'], f['src'])
        ack_delay += buf.queues[port] * TX_TIME * 0.1
        self._push(self.time + ack_delay, 'ACK', (fid, data.get('ecn', False)))

    def _handle_ack(self, ack_data):
        """ACK received at sender."""
        fid, ecn_marked = ack_data
        if fid not in self.flows:
            return

        f = self.flows[fid]
        f['acked'] += 1

        # Update round-trip time (RTT) estimate with queuing
        buf = self.leaf_buffers[f['dst_leaf']]
        qdelay = buf.queues[f['dst_port']] * TX_TIME
        f['rtt'] = self.topo.get_rtt(f['src'], f['dst']) + qdelay

        # Check completion
        if f['acked'] >= f['npkts']:
            f['active'] = False
            f['finish'] = self.time
            self.completed_flows.append(f)
            del self.flows[fid]
            return

        # TCP congestion control with ECN support (Data Center TCP (DCTCP)-like)
        if ecn_marked:
            # DCTCP: aggressive window reduction on Explicit Congestion Notification (ECN)
            f['cwnd'] = max(2, f['cwnd'] * 0.5)
            f['ssthresh'] = max(2, f['cwnd'])
        elif f['cwnd'] < f['ssthresh']:
            f['cwnd'] = min(f['cwnd'] + 1, f['npkts'])  # Slow start
        else:
            f['cwnd'] = min(f['cwnd'] + 1.0 / f['cwnd'], f['npkts'])

        f['rto_pending'] = False
        self._send_packets(f)

    def _handle_rto(self, fid):
        """Retransmission timeout."""
        if fid not in self.flows:
            return
        f = self.flows[fid]
        f['rto_pending'] = False
        f['cwnd'] = 1  # Reset window
        self._send_packets(f)

    # ---- Main Loop ----

    def run(self):
        """Run the event-driven simulation."""
        max_events = 10_000_000
        count = 0
        sample_every = 5000

        while self._heap and count < max_events:
            t, _, etype, data = heapq.heappop(self._heap)
            if t > self.duration:
                break
            self.time = t
            count += 1

            if   etype == 'FLOW_START': self._handle_flow_start(data)
            elif etype == 'PKT_ARRIVE': self._handle_pkt_arrive(data)
            elif etype == 'PKT_DEPART': self._handle_pkt_depart(data)
            elif etype == 'ACK':        self._handle_ack(data)
            elif etype == 'RTO':        self._handle_rto(data)

            # Sample buffer occupancy
            if count % sample_every == 0:
                for leaf in range(NUM_LEAVES):
                    self.occ_samples[leaf].append(
                        self.leaf_buffers[leaf].occ_pct()
                    )

        return self._collect_results()

    def _collect_results(self):
        """Compute flow completion time (FCT) slowdown metrics from completed and incomplete flows."""
        incast = []
        ws_short = []

        # Completed flows
        for f in self.completed_flows:
            fct = f['finish'] - f['start']
            ifct = ideal_fct(f['size'])
            slowdown = fct / ifct if ifct > 0 else fct
            rec = {
                'size': f['size'], 'fct': fct, 'ideal_fct': ifct,
                'slowdown': slowdown, 'is_incast': f['is_incast'],
                'drops': f['dropped'], 'src': f['src'], 'dst': f['dst'],
            }
            if f['is_incast']:
                incast.append(rec)
            elif f['size'] < 100000:
                ws_short.append(rec)

        # Incomplete flows (stuck due to drops/retransmission timeout (RTO)) count too.
        # Counting unfinished flows avoids making a scheme look artificially good
        # just because its worst flows did not finish before the time limit.
        for fid, f in self.flows.items():
            fct = self.duration - f['start']
            ifct = ideal_fct(f['size'])
            slowdown = fct / ifct if ifct > 0 else fct
            rec = {
                'size': f['size'], 'fct': fct, 'ideal_fct': ifct,
                'slowdown': slowdown, 'is_incast': f['is_incast'],
                'drops': f['dropped'], 'src': f['src'], 'dst': f['dst'],
            }
            if f['is_incast']:
                incast.append(rec)
            elif f['size'] < 100000:
                ws_short.append(rec)

        # Buffer occupancy across all leaf switches
        all_occ = []
        for leaf in range(NUM_LEAVES):
            all_occ.extend(self.occ_samples[leaf])

        tp = self.total_bytes_out * 8 / max(self.duration, 1e-9) / 1e9

        return {
            'incast': incast,
            'ws_short': ws_short,
            'occ_samples': all_occ,
            'throughput_gbps': tp,
        }


# ============================================================
# Experiment Runner
# ============================================================

def p99(records, key='slowdown'):
    if not records:
        return 1.0
    vals = [r[key] for r in records]
    return float(np.percentile(vals, 99))


def run_all_experiments(loads=None, schemes=None, n_runs=2, duration=0.04):
    if loads is None:
        loads = [20, 40, 60, 80]
    if schemes is None:
        schemes = ['DT', 'CS', 'ABM']

    topo = Topology()
    topo.print_topology()
    print()

    R = {s: {'inc': [], 'ws': [], 'buf': [], 'tp': []} for s in schemes}
    total = len(loads) * len(schemes) * n_runs
    n = 0

    for sch in schemes:
        for ld in loads:
            ri, rw, rb, rt = [], [], [], []
            for run in range(n_runs):
                n += 1
                # Keep runs reproducible while still varying load and repetition.
                rng = np.random.RandomState(42 + run * 137 + ld * 7)

                sim = Simulator(topo, scheme=sch, duration=duration)
                sim.generate_websearch(ld / 100.0, rng)
                sim.generate_incast(0.50, ld / 100.0, rng)
                results = sim.run()

                vi = p99(results['incast'])
                vw = p99(results['ws_short'])
                vb = float(np.percentile(results['occ_samples'], 99)) if results['occ_samples'] else 0
                vt = results['throughput_gbps']

                ri.append(vi); rw.append(vw); rb.append(vb); rt.append(vt)

                n_inc = len(results['incast'])
                n_ws = len(results['ws_short'])
                d_inc = np.mean([r['drops'] for r in results['incast']]) if results['incast'] else 0
                d_ws = np.mean([r['drops'] for r in results['ws_short']]) if results['ws_short'] else 0

                print(f"  [{n:>2d}/{total}] {sch:>3s} {ld}% r{run+1}: "
                      f"inc_p99={vi:>7.1f}x  ws_p99={vw:>7.1f}x  "
                      f"buf={vb:>5.1f}%  tp={vt:>4.1f}G  "
                      f"#inc={n_inc:>4d}  #ws={n_ws:>4d}  "
                      f"drops_i={d_inc:.2f}  drops_w={d_ws:.2f}")

            R[sch]['inc'].append(np.mean(ri))
            R[sch]['ws'].append(np.mean(rw))
            R[sch]['buf'].append(np.mean(rb))
            R[sch]['tp'].append(np.mean(rt))

    return loads, R


# ============================================================
# Plotting (Figure 6 layout)
# ============================================================

def plot_figure6(loads, R, path):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt

    fig, axes = plt.subplots(1, 4, figsize=(24, 6))
    fig.suptitle(
        'Figure 6 (Replication): Buffer Management under Various Loads\n'
        'ABM: Active Buffer Management in Datacenters (Addanki et al., SIGCOMM 2022)',
        fontsize=14, fontweight='bold', y=1.05)

    colors = {'DT': '#d62728', 'CS': '#1f77b4', 'ABM': '#2ca02c'}
    markers = {'DT': 'o', 'CS': '^', 'ABM': 's'}
    ss = [s for s in ['DT', 'CS', 'ABM'] if s in R]

    # (a) Incast
    for s in ss:
        axes[0].plot(loads, R[s]['inc'], colors[s], marker=markers[s], lw=2.5, ms=9, label=s)
    axes[0].set(xlabel='Load (%)', ylabel='99-pct FCT Slowdown', title='(a) Flows of incast traffic')
    axes[0].set_yscale('log'); axes[0].legend(fontsize=11); axes[0].grid(True, alpha=.3); axes[0].set_xticks(loads)

    # (b) Web-search short
    for s in ss:
        axes[1].plot(loads, R[s]['ws'], colors[s], marker=markers[s], lw=2.5, ms=9, label=s)
    axes[1].set(xlabel='Load (%)', ylabel='99-pct FCT Slowdown', title='(b) Short flows of web-search')
    axes[1].set_yscale('log'); axes[1].legend(fontsize=11); axes[1].grid(True, alpha=.3); axes[1].set_xticks(loads)

    # (c) Buffer occupancy
    for s in ss:
        axes[2].plot(loads, R[s]['buf'], colors[s], marker=markers[s], lw=2.5, ms=9, label=s)
    axes[2].set(xlabel='Load (%)', ylabel='99-pct Buffer (%)', title='(c) Buffer occupancy', ylim=(0, 105))
    axes[2].legend(fontsize=11); axes[2].grid(True, alpha=.3); axes[2].set_xticks(loads)

    # (d) Throughput
    max_tp = max(max(R[s]['tp']) for s in ss) or 1
    for s in ss:
        pct = [v / max_tp * 100 for v in R[s]['tp']]
        axes[3].plot(loads, pct, colors[s], marker=markers[s], lw=2.5, ms=9, label=s)
    axes[3].set(xlabel='Load (%)', ylabel='Avg. Throughput (%)', title='(d) Throughput', ylim=(0, 105))
    axes[3].legend(fontsize=11); axes[3].grid(True, alpha=.3); axes[3].set_xticks(loads)

    plt.tight_layout()
    plt.savefig(path, dpi=150, bbox_inches='tight')
    plt.close()
    print(f"\nPlot saved: {path}")


def print_summary(loads, R):
    ss = [s for s in ['DT', 'CS', 'ABM'] if s in R]
    print("\n" + "=" * 90)
    print("RESULTS: 99th Percentile Flow Completion Time (FCT) Slowdown")
    print("=" * 90)
    for metric, key in [('Incast Traffic', 'inc'), ('Web-Search Short Flows', 'ws')]:
        print(f"\n  {metric}:")
        hdr = f"  {'Load':>6}"
        for s in ss: hdr += f"  {s:>8}"
        if 'ABM' in R and 'DT' in R: hdr += f"  {'ABM/DT':>8}"
        print(hdr)
        for i, ld in enumerate(loads):
            line = f"  {ld:>5}%"
            for s in ss: line += f"  {R[s][key][i]:>7.1f}x"
            if 'ABM' in R and 'DT' in R:
                d = R['DT'][key][i]; a = R['ABM'][key][i]
                imp = (d - a) / d * 100 if d > 0 else 0
                line += f"  {imp:>+7.1f}%"
            print(line)

    print(f"\n  Buffer Occupancy (p99 %):")
    for i, ld in enumerate(loads):
        parts = [f"  {ld:>5}%"]
        for s in ss: parts.append(f"{s}={R[s]['buf'][i]:.0f}%")
        print("  ".join(parts))
    print("=" * 90)


# ============================================================
# Main
# ============================================================

if __name__ == '__main__':
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except (OSError, ValueError):
            pass
    print("=" * 70)
    print("ABM: Active Buffer Management - Figure 6 Replication")
    print("CIS 437/537 Final Project - Fnu Nidhi (UMID: 92553817)")
    print("=" * 70)
    print(f"  Simulator: Event-driven packet-level with Leaf-Spine topology")
    print(f"  Buffer: {SHARED_BUF} pkts shared per leaf | alpha={ALPHA} alpha_u={ALPHA_U}")
    print(f"  Links: {LINK_BW/1e9:.0f}G, {LINK_DELAY*1e6:.0f}us | RTT (round-trip time): {RTT_BASE*1e6:.0f}us | RTO (retransmission timeout): {MIN_RTO*1e3:.0f}ms")
    print()

    loads, R = run_all_experiments(
        loads=[20, 40, 60, 80],
        schemes=['DT', 'CS', 'ABM'],
        n_runs=2,
        duration=0.06
    )

    print_summary(loads, R)

    fig_path = os.path.join(_PROJECT_DIR, 'figure6_replication.png')
    plot_figure6(loads, R, fig_path)

    with open(os.path.join(_PROJECT_DIR, 'results.json'), 'w') as f:
        json.dump({'loads': loads, **R}, f, indent=2)

    print("\nSimulation complete!")
