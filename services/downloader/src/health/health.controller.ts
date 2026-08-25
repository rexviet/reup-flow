import { Controller, Get } from "@nestjs/common";
import { Public } from "../auth/public.decorator";

@Controller("health")
export class HealthController {
  // MUST stay open: docker-compose.yml's healthcheck probes this route with
  // `wget` and no header (see test/compose-constraints.spec.ts), and it is
  // also the only thing an operator has to look at before a valid API_KEY
  // exists at all.
  @Public()
  @Get()
  check(): { status: "ok" } {
    return { status: "ok" };
  }
}
