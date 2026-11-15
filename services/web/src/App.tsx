import { useEffect, useState } from 'react';
import { AppShell } from './components/AppShell';
import { DashboardPage } from './dashboard/DashboardPage';
import { VideoDetail } from './dashboard/VideoDetail';

type Route = { page: 'dashboard' } | { page: 'video'; awemeId: string };

/** `#/video/<aweme>` is the detail page; anything else is the dashboard. Hash routing keeps nginx config out of it. */
export function routeFromHash(hash: string): Route {
  const match = /^#\/video\/(\d{1,32})$/.exec(hash);
  return match ? { page: 'video', awemeId: match[1] } : { page: 'dashboard' };
}

export default function App() {
  const [route, setRoute] = useState<Route>(() => routeFromHash(window.location.hash));

  useEffect(() => {
    const onHash = () => setRoute(routeFromHash(window.location.hash));
    window.addEventListener('hashchange', onHash);
    return () => window.removeEventListener('hashchange', onHash);
  }, []);

  return (
    <AppShell awemeId={route.page === 'video' ? route.awemeId : null}>
      {route.page === 'video' ? <VideoDetail key={route.awemeId} awemeId={route.awemeId} /> : <DashboardPage />}
    </AppShell>
  );
}
