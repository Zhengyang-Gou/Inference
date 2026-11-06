"""Reviewed overlays applied only to the isolated, pinned flowSim build.

The vendored tree under ``third_party/flowsim`` keeps the exact upstream bytes
(checked by ``SOURCE_MANIFEST.json``). Everything this adapter needs beyond the
upstream offline batch entry point is expressed here, with an exact-match
assertion per replacement, so the patch is auditable and cannot silently drift:

1. ``EventQueue`` gains a read-only peek at the next event time and an explicit
   idle clock advance. Upstream only exposes a blocking ``proceed()``; an online
   adapter must be able to advance to a deadline that no event reaches (for
   example a sampling timer the caller knows about but has not submitted).
2. ``EventQueue::proceed`` accepts an event list whose timestamp equals the
   current time. Upstream asserts strict progress because its own trace never
   schedules two flows at the same nanosecond; online submissions do, and a
   zero-delay timer must fire at the current instant.
3. ``Chunk`` carries the path propagation latency. Upstream stores ``latency``
   on ``Link`` but never reads it, so every completion is pure serialisation
   time and small flows finish within 1 ns.
4. ``Topology::reschedule_active_chunks`` adds that path latency once per
   completion plan. ``remaining/rate`` stays the serialisation term, so a
   re-plan does not accumulate latency.
5. ``EventList::invoke_event`` copies the entry before popping it. Upstream keeps
   a reference to the list node and calls the callback after ``pop_front()`` has
   destroyed it: a heap-use-after-free that AddressSanitizer reports on the
   first realistic run and that corrupts callbacks under allocation churn.
6. ``Topology::cancel_all_events`` stops cancelling event id 0. Upstream uses 0
   both as the first scheduled id and as the "no planned completion" marker;
   after a list is popped, cancelling the marker dereferences a stale iterator
   (a second heap-use-after-free, again found by AddressSanitizer).
7. ``EventQueue`` starts ids at 1 so that 0 is unambiguously "no planned
   completion". With ids from 0, the first scheduled completion in a run is id
   0: guarding the marker in (6) would then refuse to cancel that flow's stale
   completion event and the flow would keep its pre-contention finish time.
8. ``Topology`` gains ``enqueue``/``settle``: add flows without re-running the
   progressive filling, then recompute once for the whole batch. Upstream
   ``send`` reallocates bandwidth for every arrival, which is O(active^2) per
   flow; an online trace submits hundreds of thousands of flows at the same
   instant, so the unbatched form never finishes. The arithmetic of the
   allocation is unchanged, only its batching.
"""
from pathlib import Path


def _edit(root, relative, replacements):
    path = Path(root) / relative
    text = path.read_text()
    for old, new in replacements:
        if text.count(old) != 1:
            raise ValueError("Pinned flowSim overlay no longer matches: " + relative)
        text = text.replace(old, new)
    path.write_text(text)


def apply_flowsim_overlay(source):
    source = Path(source)

    _edit(source, "EventQueue.h", [(
        "  void log_events();\n",
        "  void log_events();\n"
        "\n"
        "  /**\n"
        "   * Time of the next scheduled event; the current time once empty.\n"
        "   */\n"
        "  [[nodiscard]] EventTime next_event_time() const noexcept;\n"
        "\n"
        "  /**\n"
        "   * Move the clock forward without invoking events. Only valid when no\n"
        "   * event is scheduled at or before the target time.\n"
        "   */\n"
        "  void advance_to(EventTime event_time) noexcept;\n",
    )])

    _edit(source, "EventQueue.cpp", [
        ("EventQueue::EventQueue() noexcept : current_time(0), next_event_id(0) {",
         "EventQueue::EventQueue() noexcept : current_time(0), next_event_id(1) {"),
        ("  // Check the validity and update current time\n"
         "  assert(current_event_list.get_event_time() > current_time);\n",
         "  // Check the validity and update current time. Equal timestamps are\n"
         "  // legal: events that a callback schedules at the current instant are\n"
         "  // appended to this same list and must run before the list is dropped.\n"
         "  assert(current_event_list.get_event_time() >= current_time);\n"),
        ("void EventQueue::cancel_event(EventId event_id) noexcept {",
         "EventTime EventQueue::next_event_time() const noexcept {\n"
         "  return event_queue.empty() ? current_time : event_queue.front().get_event_time();\n"
         "}\n"
         "\n"
         "void EventQueue::advance_to(EventTime event_time) noexcept {\n"
         "  assert(event_time >= current_time);\n"
         "  assert(event_queue.empty() || event_queue.front().get_event_time() > event_time);\n"
         "  current_time = event_time;\n"
         "}\n"
         "\n"
         "void EventQueue::cancel_event(EventId event_id) noexcept {"),
    ])

    _edit(source, "Chunk.h", [
        ("  [[nodiscard]] EventTime get_transmission_start_time() const noexcept;\n",
         "  [[nodiscard]] EventTime get_transmission_start_time() const noexcept;\n"
         "  void set_latency(double value) noexcept;\n"
         "  [[nodiscard]] double get_latency() const noexcept;\n"),
        ("  EventTime transmission_start_time;\n",
         "  EventTime transmission_start_time;\n"
         "  double propagation_latency;\n"),
    ])

    _edit(source, "Chunk.cpp", [
        ("      callback(callback), callback_arg(callback_arg), transmission_start_time(0), rate(0),"
         " completion_event_id_(0), topology(nullptr) {",
         "      callback(callback), callback_arg(callback_arg), transmission_start_time(0), rate(0),"
         " completion_event_id_(0), topology(nullptr), propagation_latency(0) {"),
        ("EventTime Chunk::get_transmission_start_time() const noexcept {",
         "void Chunk::set_latency(double value) noexcept {\n"
         "  propagation_latency = value;\n"
         "}\n"
         "\n"
         "double Chunk::get_latency() const noexcept {\n"
         "  return propagation_latency;\n"
         "}\n"
         "\n"
         "EventTime Chunk::get_transmission_start_time() const noexcept {"),
    ])

    _edit(source, "Topology.cpp", [(
        "            double new_completion_time = std::max(1.0, (remaining_size / new_rate));\n",
        "            // Fluid serialisation plus one path propagation delay. The\n"
        "            // serialisation term alone makes tiny flows complete in <= 1 ns.\n"
        "            // A chunk whose bytes all fit in the elapsed interval (for example\n"
        "            // a sibling that was cancelled by this completion) must finish at\n"
        "            // once instead of re-paying the propagation delay.\n"
        "            double new_completion_time = remaining_size > 0\n"
        "                ? std::max(1.0, (remaining_size / new_rate) + chunk->get_latency())\n"
        "                : 1.0;\n",
    )])

    _edit(source, "EventList.cpp", [(
        "  auto const& event = events.front();\n"
        "  events.pop_front();\n"
        "  event.callback(event.callback_arg);\n",
        "  // Copy before popping; the callback must not read a destroyed node.\n"
        "  const EventEntry event = events.front();\n"
        "  events.pop_front();\n"
        "  event.callback(event.callback_arg);\n",
    )])

    _edit(source, "Topology.cpp", [(
        "                chunk->update_remaining_size(transmitted_size);\n"
        "                event_queue->cancel_event(chunk->get_completion_event_id());\n"
        "                chunk->set_completion_event_id(0);\n",
        "                chunk->update_remaining_size(transmitted_size);\n"
        "                // Event id 0 doubles as \"no planned completion\". Cancelling\n"
        "                // the marker would look up the first-ever event, whose list\n"
        "                // was already dropped, and dereference a stale iterator.\n"
        "                if (const EventId pending = chunk->get_completion_event_id())\n"
        "                    event_queue->cancel_event(pending);\n"
        "                chunk->set_completion_event_id(0);\n",
    )])

    _edit(source, "Topology.h", [(
        "  void send(std::unique_ptr<Chunk> chunk) noexcept;\n",
        "  void send(std::unique_ptr<Chunk> chunk) noexcept;\n"
        "  // Batched submission: enqueue any number of chunks, then settle once.\n"
        "  void enqueue(std::unique_ptr<Chunk> chunk) noexcept;\n"
        "  void settle() noexcept;\n",
    )])

    _edit(source, "Topology.cpp", [(
        "    add_chunk_to_links(chunk_ptr);\n"
        "    update_link_states();\n"
        "    reschedule_active_chunks();\n"
        "}\n",
        "    add_chunk_to_links(chunk_ptr);\n"
        "    update_link_states();\n"
        "    reschedule_active_chunks();\n"
        "}\n"
        "\n"
        "void Topology::enqueue(std::unique_ptr<Chunk> chunk) noexcept {\n"
        "  assert(chunk != nullptr);\n"
        "  chunk->set_topology(this);\n"
        "  active_chunks_ptrs.push_back(std::move(chunk));\n"
        "  Chunk* chunk_ptr = active_chunks_ptrs.back().get();\n"
        "  active_chunks.push_back(chunk_ptr);\n"
        "  add_chunk_to_links(chunk_ptr);\n"
        "}\n"
        "\n"
        "void Topology::settle() noexcept {\n"
        "  if (active_chunks.empty()) return;\n"
        "  cancel_all_events();\n"
        "  update_link_states();\n"
        "  reschedule_active_chunks();\n"
        "}\n",
    )])
