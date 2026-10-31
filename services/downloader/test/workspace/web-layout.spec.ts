import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { AGENTS } from '../../src/workspace/workspace.catalog';
import { GATE_OUTCOMES } from '../../src/workspace/workspace.schemas';

/**
 * The web floor plan lists its desks by id. It cannot import the catalog (the web
 * image is built without this package), so this test reads the file instead and
 * fails when the two lists drift apart.
 */
const layout = readFileSync(resolve(__dirname, '../../../web/src/workspace/workspace.layout.ts'), 'utf8');

describe('web workspace layout matches the catalog', () => {
  it('has a desk for every catalog agent and no other, in the same zone', () => {
    const zones = [...layout.matchAll(/zone: '(\w+)',\s+title: '[^']*',\s+desks: \[([\s\S]*?)\],\s+\}/g)];
    const fromLayout = zones.flatMap(([, zone, desks]) =>
      [...desks.matchAll(/id: '(\w+)'/g)].map(([, id]) => `${zone}:${id}`),
    );
    expect(fromLayout.sort()).toEqual(AGENTS.map((a) => `${a.zone}:${a.id}`).sort());
  });

  it('has a gate column for every gate outcome', () => {
    const body = layout.slice(layout.indexOf('GATE_COLUMNS'));
    const ids = [...body.matchAll(/outcome: '(\w+)'/g)].map(([, o]) => o);
    expect(ids.sort()).toEqual([...GATE_OUTCOMES].sort());
  });
});
