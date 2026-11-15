import {
  activitiesFromHistory,
  pendingFromDescribe,
  tsToMs,
  type HistoryEventLike,
} from '../../src/workspace/workspace.temporal';

const ts = (ms: number) => ({ seconds: { toString: () => String(Math.floor(ms / 1000)) }, nanos: (ms % 1000) * 1e6 });

describe('tsToMs', () => {
  it('reads a Long-like seconds value, as protobufjs decodes int64', () => {
    expect(tsToMs(ts(1_793_426_758_576))).toBe(1_793_426_758_576);
  });
  it('is undefined when the timestamp is missing', () => {
    expect(tsToMs(null)).toBeUndefined();
    expect(tsToMs({})).toBeUndefined();
  });
});

describe('activitiesFromHistory', () => {
  const events: HistoryEventLike[] = [
    { eventId: 5, eventTime: ts(1000), activityTaskScheduledEventAttributes: { activityType: { name: 'ingest' }, taskQueue: { name: 'media' } } },
    { eventId: 6, eventTime: ts(2000), activityTaskScheduledEventAttributes: { activityType: { name: 'render' }, taskQueue: { name: 'render' } } },
    { eventId: 7, eventTime: ts(1500), activityTaskScheduledEventAttributes: { activityType: { name: 'transcribe' }, taskQueue: { name: 'stt' } } },
    { eventId: 9, eventTime: ts(4000), activityTaskCompletedEventAttributes: { scheduledEventId: 5 } },
    {
      eventId: 10,
      eventTime: ts(5000),
      activityTaskFailedEventAttributes: {
        scheduledEventId: 6,
        failure: { applicationFailureInfo: { type: 'PermanentProviderError' } },
      },
    },
    { eventId: 11, eventTime: ts(6000), activityTaskTimedOutEventAttributes: { scheduledEventId: 7 } },
  ];

  it('folds scheduled, completed, failed and timed-out events into one record per activity, in order', () => {
    expect(activitiesFromHistory(events)).toEqual([
      { eventId: 5, name: 'ingest', queue: 'media', scheduledAtMs: 1000, completedAtMs: 4000 },
      { eventId: 7, name: 'transcribe', queue: 'stt', scheduledAtMs: 1500, failedAtMs: 6000, failureType: 'TimeoutError' },
      { eventId: 6, name: 'render', queue: 'render', scheduledAtMs: 2000, failedAtMs: 5000, failureType: 'PermanentProviderError' },
    ]);
  });

  it('keeps an activity with no outcome yet as a scheduled record', () => {
    const [only] = activitiesFromHistory([events[0]]);
    expect(only).toEqual({ eventId: 5, name: 'ingest', queue: 'media', scheduledAtMs: 1000 });
  });

  it('drops a failure class that is not an identifier instead of forwarding free text', () => {
    const [record] = activitiesFromHistory([
      events[0],
      {
        eventId: 8,
        eventTime: ts(3000),
        activityTaskFailedEventAttributes: {
          scheduledEventId: 5,
          failure: { applicationFailureInfo: { type: 'oops: prompt was "secret text"' } },
        },
      },
    ]);
    expect(record.failureType).toBe('ActivityFailure');
  });
});

describe('pendingFromDescribe', () => {
  it('uses the last attempt start, else the schedule time, and floors attempt at 1', () => {
    const pending = pendingFromDescribe([
      { activityId: '3', activityType: { name: 'ingest' }, attempt: 0, scheduledTime: ts(1000) },
      {
        activityId: '4',
        activityType: { name: 'render' },
        attempt: 3,
        scheduledTime: ts(1000),
        lastStartedTime: ts(9000),
        lastFailure: { applicationFailureInfo: { type: 'ConnectionError' } },
      },
    ]);
    expect(pending).toEqual([
      { activityId: '3', name: 'ingest', attempt: 1, atMs: 1000, failureType: undefined },
      { activityId: '4', name: 'render', attempt: 3, atMs: 9000, failureType: 'ConnectionError' },
    ]);
  });
  it('is empty for no pending activities', () => {
    expect(pendingFromDescribe(undefined)).toEqual([]);
  });
});
