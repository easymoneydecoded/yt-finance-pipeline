// Despertador de respaldo: GitHub a veces retrasa horas o se salta sus crons (sobre todo en fin de semana).
// - A las :10 de cada franja (15, 19 y 23 UTC) pide a GitHub que ejecute social.yml (Shorts). Si GitHub ya
//   publico esa franja, social_publisher.py lo sabe (history/social_slots.json) y no repite.
// - A las 9:35 UTC lanza el episodio diario de Zibu (zibu-stories/daily.yml con auto=true): si hoy ya hay
//   episodio, el workflow no hace nada. En los dos, el grupo de concurrencia evita ejecuciones a la vez.
// - Domingo y miercoles a las 16:20 UTC, los posts de Reddit (alerts.yml): insider_reddit.py recuerda lo
//   enviado cada semana (reddit_sent) y no lo repite si GitHub tambien lanzo su cron de las 16:07.
// - Martes a sabado a las 03:50 UTC, el resumen nocturno (alerts.yml recap: Telegram + web + redes). El dia del
//   resumen sale de la hora de Nueva York (recap_day) y queda marcado, asi que no se repite con el de GitHub.
const JOBS = {
  "35 9 * * *": { repo: "easymoneydecoded/zibu-stories", workflow: "daily.yml", inputs: { auto: "true" } },
  "20 16 * * 0": { repo: "easymoneydecoded/yt-finance-pipeline", workflow: "alerts.yml", inputs: { mode: "reddit" } },
  "50 3 * * 2-6": { repo: "easymoneydecoded/yt-finance-pipeline", workflow: "alerts.yml", inputs: { mode: "recap" } },
  "20 16 * * 3": { repo: "easymoneydecoded/yt-finance-pipeline", workflow: "alerts.yml", inputs: { mode: "midweek" } },
  default: { repo: "easymoneydecoded/yt-finance-pipeline", workflow: "social.yml", inputs: undefined },
};

export default {
  async scheduled(event, env, ctx) {
    const job = JOBS[event.cron] || JOBS.default;
    const r = await fetch(`https://api.github.com/repos/${job.repo}/actions/workflows/${job.workflow}/dispatches`, {
      method: "POST",
      headers: {
        Authorization: `Bearer ${env.GH_TOKEN}`,
        Accept: "application/vnd.github+json",
        "User-Agent": "tickerpress-cron",
      },
      body: JSON.stringify({ ref: "main", ...(job.inputs ? { inputs: job.inputs } : {}) }),
    });
    console.log(`dispatch ${job.repo}/${job.workflow}:`, r.status, await r.text());
  },
};
