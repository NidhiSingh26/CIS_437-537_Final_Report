# Replicating ABM (Active Buffer Management) (SIGCOMM 2022) — Figure 6

**Course:** CIS 437 / 537 — Final report  
**Author:** Fnu Nidhi (UMID: 92553817)

This repository contains a self-contained replication study of one figure from *ABM: Active Buffer Management in Datacenters* (Addanki, Munir, Hasan, Ghobadi, Alizadeh — ACM Special Interest Group on Data Communication (SIGCOMM) 2022, [https://dl.acm.org/doi/10.1145/3544216.3544252](https://dl.acm.org/doi/10.1145/3544216.3544252)).

---

## 1. Introduction: the paper and why it matters

The paper studies a core problem in datacenter networking: many applications generate bursts of packets that arrive at the same switch output port at nearly the same time. This pattern, usually called **incast**, can overflow shared switch buffers and create synchronized packet loss. When that happens, **flow completion time (FCT)** increases sharply, especially for latency-sensitive short flows such as web-search traffic. Because modern datacenters depend on predictable tail latency, this is an important systems problem rather than just a small queueing issue.

**ABM (Active Buffer Management)** is the paper's proposed solution. Instead of letting all ports compete for shared buffer space with a simple static rule, ABM adjusts admission dynamically based on:

- how much total buffer remains,
- how many output ports are currently congested, and
- how quickly each port is draining traffic.

ABM also distinguishes between **unscheduled** traffic, which represents the initial burst of an incast, and **scheduled** traffic, which has already entered normal congestion-control behavior. For scheduled traffic, it prefers **Explicit Congestion Notification (ECN)** marking over immediate packet drops, which fits naturally with **Data Center TCP (DCTCP)**-style senders.

The paper's main contribution is showing that better buffer admission logic can substantially reduce tail latency without giving up throughput. This matters because shared-buffer switches are common in datacenters, and buffer-management policy directly affects application performance. In short, the paper is important because it connects a low-level switch mechanism to user-visible performance metrics such as tail FCT and throughput.

---

## 2. Result chosen and rationale

**Chosen result:** **Figure 6** in the paper — *buffer management under various loads*, with four panels:

**Why this figure:** I chose Figure 6 because it is the clearest summary of the paper's main claim. Instead of focusing on one narrow metric, it compares **Dynamic Thresholds (DT)**, **Complete Sharing (CS)**, and **Active Buffer Management (ABM)** across multiple offered loads and across four complementary views: incast tail latency, web-search tail latency, buffer occupancy, and throughput. Reproducing this figure is a strong test of whether the implementation captures the paper's core idea.

This figure is also a good choice for a replication project because it forces the simulator to combine the paper's main components: the shared-buffer policy, multiple traffic types, load variation, and sender-side reaction to congestion. If the implementation were wrong in an important way, it would likely show up somewhere in this four-panel comparison.

---

## 3. Methodology described in the paper (summary)

From the paper's evaluation setup (Section 4 and the Figure 6 discussion), the methodology can be summarized as follows.

The network is a **leaf-spine datacenter topology** in which congestion is most relevant at the **leaf switch egress queues**, where multiple senders may compete for the same output port. The switch uses a **shared buffer**, and the paper compares three policies for deciding how that shared buffer is allocated:

- **Dynamic Thresholds (DT)**, which scales queue admission based on remaining shared space,
- **Complete Sharing (CS)**, which allows any port to consume essentially all remaining shared buffer, and
- **ABM**, which further adjusts admission using the number of congested ports and the normalized drain rate of each port.

The workloads combine two important datacenter traffic patterns. The first is **web-search traffic**, whose flow sizes are drawn from the **Data Center TCP (DCTCP)** web-search **cumulative distribution function (CDF)**. The second is **incast traffic**, where many senders simultaneously transmit to one receiver. This workload mix is useful because it creates tension between throughput-oriented long or bursty flows and latency-sensitive short flows.

The primary reported metrics are:

- **99th percentile flow completion time (FCT) slowdown**, measured as actual FCT divided by ideal FCT,
- **buffer occupancy**, and
- **throughput**.

These metrics are plotted against offered load, typically from **20% to 80%**, so the reader can see how each policy behaves under increasingly stressful conditions. The paper's original results come from packet-level simulation with a **10 gigabits per second (Gbps)** environment and **DCTCP** senders.

---

## 4. Methodology used in this repository (and where it diverges)

### 4.1 What we implemented

All logic is in [`abm_simulation.py`](abm_simulation.py):

This repository implements a **custom event-driven packet-level simulator in Python**. The goal was to reproduce the logic of the paper closely enough to test the same claim, while keeping the code understandable and runnable for a course project.

More concretely, the implementation does the following:

- models a **2-leaf, 2-spine** topology with **8 hosts per leaf**,
- uses **10 Gbps** links with **10 microseconds (µs)** of propagation delay per hop,
- places the key queueing point at the **destination leaf egress port**, where the shared-buffer policy matters,
- represents a **shared buffer of 40 packets** per leaf switch,
- implements the three schemes **DT**, **CS**, and **ABM**,
- generates **web-search traffic** using a Poisson arrival process and the **DCTCP web-search cumulative distribution function (CDF)** for flow sizes,
- generates **incast traffic** by having all senders on one leaf transmit nearly simultaneously to one receiver on the other leaf,
- models sender feedback with a simplified **TCP-like** congestion-control loop with acknowledgments, additive increase/multiplicative decrease (AIMD), **Explicit Congestion Notification (ECN)** response, and **retransmission timeout (RTO)** after drops.

The ABM-specific logic follows the paper's main ideas. The code estimates the number of congested ports \(n_p\), scales admission by remaining shared space, and weights admission by the estimated port drain rate. It also treats the first burst of incast as **unscheduled** traffic and uses **ECN** for scheduled traffic, which preserves the paper's intended distinction between drop-based and mark-based reactions.

### 4.2 Deliberate simplifications vs the paper

| Aspect | Paper (typical) | This replication |
|--------|-----------------|------------------|
| Simulator | ns-3 / production-faithful TCP | Custom discrete-event model |
| TCP | DCTCP (precise \(\alpha\), marking) | **DCTCP-like** ECN reaction (multiplicative decrease on ECN) |
| Time scale | Long runs for stable percentiles | Default **0.06 s** wall time, **2 runs** averaged (configurable) |
| Topology scale | Larger fat-tree / more hosts | **16 hosts**, 2 leaves (stress incast but not at data-center scale) |
| Buffer / load calibration | Tuned to match hardware | Fixed **40 packets**; load from **offered web-search rate + incast rate** |

These differences matter, so they should be stated clearly. This project is a **replication of the paper's result at the level of behavior and comparison**, not a claim that the simulator is identical to the authors' original environment.

**Why these divergences:** A full ns-3 rebuild was outside the scope of this course project. The practical goal here was to encode the paper's buffer-management ideas and workload patterns in a transparent simulator, then check whether the **relative ordering** of ABM, DT, and CS follows the same qualitative pattern as the paper. Because the transport model, scale, and runtime are simplified, the **absolute numerical values** should not be expected to match the paper exactly.

---

## 5. Results: ours vs the paper (how to compare)

### 5.1 Axes alignment (same as Figure 6)

The script [`plot_figure6`](abm_simulation.py) emits a **1×4** panel figure saved as [`figure6_replication.png`](figure6_replication.png):

- **X-axis:** offered load **20%, 40%, 60%, 80%** (same idea as the paper’s load sweep).
- **(a)(b) Y-axis:** **99th percentile flow completion time (FCT) slowdown**, **log** scale.
- **(c) Y-axis:** **99th percentile buffer occupancy (%)**, 0–100.
- **(d) Y-axis:** **Average throughput** as **percent of the best scheme’s throughput** at each load (same normalization style as “relative throughput” in comparative plots).

**Reading the original:** Open the paper's Figure 6 alongside `figure6_replication.png` and compare **curve shapes**, **relative separation**, and **ordering of DT / CS / ABM**, rather than expecting literal equality in the y-values. This is the most useful way to compare the original and the replicated result.

### 5.2 Numerical results from this repo (default script settings)

The tables below correspond to the **default script settings** in `abm_simulation.py` (simulation **0.06 s** per run, **2 runs averaged** per (scheme, load) point, loads **20–80%**). Re-running will change exact values because of randomness.

When you run the script, it will also write a machine-readable `results.json` next to `abm_simulation.py` with the same structure used for plotting.

**99th percentile FCT slowdown — incast (a)**

| Load | DT | CS | ABM |
|------|-----|-----|-----|
| 20% | 1292× | 962× | 397× |
| 40% | 1331× | 1291× | 614× |
| 60% | 1343× | 1334× | 1197× |
| 80% | 1394× | 1391× | 1147× |

**99th percentile FCT slowdown — short web-search (b)**

| Load | DT | CS | ABM |
|------|-----|-----|-----|
| 20% | 770× | 732× | **25×** |
| 40% | 947× | 920× | 457× |
| 60% | 988× | 983× | 775× |
| 80% | 1026× | 999× | 870× |

**99th percentile buffer occupancy % (c)**

| Load | DT | CS | ABM |
|------|-----|-----|-----|
| 20% | 47% | 64% | 46% |
| 40% | 57% | 69% | 78% |
| 60% | 63% | 79% | 81% |
| 80% | 68% | 79% | 80% |

**Throughput (simulator Gbps — used for panel (d) normalization)**

| Load | DT | CS | ABM |
|------|-----|-----|-----|
| 20% | 19.5 | 13.7 | 29.6 |
| 40% | 29.9 | 28.7 | 48.5 |
| 60% | 38.3 | 38.8 | 50.9 |
| 80% | 43.4 | 42.8 | 53.0 |

### 5.3 Comparison to the paper’s message

The strongest point of comparison is not the exact value in each table cell, but the **overall story** told by the results.

- **Incast under DT remains bad.** In the replicated results, DT produces very large tail slowdowns for incast across all loads. This agrees with the paper's basic argument that simple shared-buffer policies handle incast poorly.
- **ABM helps most clearly at lower to moderate loads.** In our run, ABM reduces 99th percentile incast slowdown relative to DT by about **69% at 20% load** and **54% at 40% load**. That is directionally consistent with the paper's claim that ABM better protects latency during incast.
- **Short-flow performance also improves under ABM.** The biggest gain appears at **20% load**, where ABM gives a much lower web-search tail slowdown than DT or CS. This is important because one of the paper's motivations is protecting short, latency-sensitive traffic.
- **At high load, the separation narrows.** In the replicated simulator, the ABM advantage becomes smaller at **60% and 80% load**, especially for incast. This is one of the main places where our simplified model differs from the stronger separation suggested by the original paper.
- **Throughput remains competitive or better under ABM.** ABM achieves the highest raw throughput values in our results while still improving short-flow and incast behavior in several settings, which is consistent with the paper's claim that better tail behavior does not have to come at the expense of throughput.

**Where the replication does not fully agree with the paper:** the paper suggests a cleaner separation between ABM and the baseline schemes across load levels than we observe here. In particular, our simulator still shows very large slowdowns at high load, even under ABM. I interpret this as a consequence of simplifications in topology size, run duration, and transport behavior rather than evidence against the paper's claim.

For that reason, this project should be described as a **qualitative replication of Figure 6**. It reproduces the paper's comparison structure and much of its directional behavior, but not the exact numeric outcomes.

---

## 6. Discussion: replication process and disagreements

This section explains what happened during the replication process and why some results differ from the paper.

1. **Tail metrics are naturally noisy.** The 99th percentile is sensitive to rare events. In this project, even with two averaged runs, the exact tail value depends on how many incast episodes happen during the simulation window and how many flows complete before the run ends. Longer simulations and more seeds would make the percentile estimates more stable.

2. **The transport model is simplified.** The code includes a DCTCP-like reaction to ECN and a simplified congestion window model, but it is not a full faithful implementation of the paper's original end-host stack. Because of that, the absolute values of slowdown should be treated cautiously.

3. **Scale is much smaller than a full datacenter study.** The simulator uses 16 hosts and a very small topology so that the experiment stays understandable and runnable on a normal laptop. This makes the experiment practical, but it also means queueing behavior may be more bursty and less statistically smooth than in the original environment.

4. **Some implementation choices were necessary.** For example, the ABM implementation includes an extra critical-fullness guard to prevent unrealistic overflow in the discrete event model. That is a reasonable engineering choice, but it is still a divergence from an idealized reproduction of the paper.

5. **The code was adjusted for reproducibility on this machine.** Output paths are now relative to the script location, and console output was cleaned up to work on Windows terminals. These changes do not alter the core experiment, but they make the project easier to run and submit.

Overall, I believe the disagreement between my numbers and the paper's numbers comes primarily from **simulation fidelity and scale**, not from misunderstanding the paper's main idea. The direction of the results is often similar, especially at low and medium load, which supports the conclusion that the core ABM idea was implemented successfully.

---

## 7. Lessons for others reading ABM

There are several practical lessons from this replication that may help another reader understand the paper in context.

- **The distinction between unscheduled and scheduled traffic is essential.** If that distinction is removed or implemented incorrectly, ABM starts to behave much more like an ordinary ECN-based shared-buffer scheme, and the specific incast protection the paper emphasizes becomes much weaker.
- **Estimating \(n_p\) is more important than it first appears.** The paper's formula depends on how many ports are considered congested, so even a small change in that rule can shift admission thresholds noticeably.
- **Drain-rate estimation is a modeling choice, not just a coding detail.** Updating too aggressively makes the policy unstable; updating too slowly makes it unresponsive. The simulator uses a round-trip-time-based update with an exponentially weighted moving average as a reasonable compromise.
- **Replication should focus on the paper's claim, not only on matching numbers.** In systems work, especially with packet-level simulation, reproducing the exact numeric curve may be less realistic than reproducing the same comparative conclusion under a transparent model.
- **It helps to compare figures structurally.** The most useful question is not "Did every point match exactly?" but rather "Do the curves tell the same story about which scheme is better, under what conditions, and why?"

---

## Repository layout

| File | Purpose |
|------|---------|
| [`abm_simulation.py`](abm_simulation.py) | Full simulator, experiment driver, plotting |
| [`figure6_replication.png`](figure6_replication.png) | Generated four-panel figure (re-run script to refresh) |
| `ABM_Presentation.pptx` | Slides for the course presentation |

### Generated files (created when you run the script)

| File | Purpose |
|------|---------|
| `results.json` | Structured numbers for the figure/tables (created by `python abm_simulation.py`) |

---

## How to run

```bash
python -m pip install numpy matplotlib
python abm_simulation.py
```

This prints per-configuration progress and a text summary, and writes **`figure6_replication.png`** and `results.json` in this directory.

**Parameters to tune** (in `abm_simulation.py`): `SHARED_BUF`, `duration`, `n_runs` in `run_all_experiments(...)`, incast `request_pct` and interval logic, and `loads=[...]`.

---

## References

Addanki, S., Munir, T., Hasan, S. T., Ghobadi, M., & Alizadeh, M. (2022). *ABM: Active Buffer Management in Datacenters.* Proceedings of the ACM SIGCOMM 2022 Conference. [https://dl.acm.org/doi/10.1145/3544216.3544252](https://dl.acm.org/doi/10.1145/3544216.3544252)

Alizadeh, M., et al. (2010). *Data Center TCP (DCTCP).* SIGCOMM (web-search flow size CDF used in this code).
