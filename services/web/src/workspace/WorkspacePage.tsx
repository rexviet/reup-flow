import { useState } from 'react';
import { EventLog } from './EventLog';
import { OfficeMap } from './OfficeMap';
import { RunPanel } from './RunPanel';
import { TopBar } from './TopBar';
import { useEventFeed } from './useEventFeed';
import { useWorkspaceSnapshot } from './useWorkspaceSnapshot';

export function WorkspacePage() {
  const { snapshot, loading, staleSeconds } = useWorkspaceSnapshot();
  const feed = useEventFeed(snapshot);
  const [selected, setSelected] = useState<{ awemeId: string; generation: number } | null>(null);

  return (
    <div className="flex flex-col gap-4">
      <TopBar snapshot={snapshot} staleSeconds={staleSeconds} loading={loading} />
      <div className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_20rem]">
        <OfficeMap
          snapshot={snapshot}
          bubbles={feed.bubbles}
          handoffs={feed.handoffs}
          selected={selected}
          onSelect={(awemeId, generation) => setSelected({ awemeId, generation })}
        />
        <div className="flex flex-col gap-4">
          {selected && (
            <RunPanel
              key={`${selected.awemeId}:${selected.generation}`}
              awemeId={selected.awemeId}
              generation={selected.generation}
              onClose={() => setSelected(null)}
            />
          )}
          <EventLog events={feed.log} />
        </div>
      </div>
    </div>
  );
}
