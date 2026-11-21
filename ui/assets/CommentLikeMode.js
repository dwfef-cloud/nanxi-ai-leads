// Each mode displays its own latest persisted run. A simulation can never be
// relabeled as a real completion or supply real counters.
export function projectCommentLikeTask(record, modes = {}, offline = false) {
  const mode = offline ? 'offline' : 'live';
  const latest = modes[mode];
  const sameMode = record.executionMode === mode;
  return {...record, executionMode: mode,
    status: latest?.status || (sameMode ? record.status : 'pending'),
    startedAt: latest?.startedAt || (sameMode ? record.startedAt : null),
    endedAt: latest?.endedAt || (sameMode ? record.endedAt : null),
    stats: latest?.stats || (sameMode ? (offline ? record.simulationStats : record.stats) : {}) || {},
    runs: (offline ? record.simulationRuns : record.runs) || []};
}
