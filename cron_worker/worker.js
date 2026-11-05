// Despertador de respaldo: GitHub a veces retrasa horas o se salta sus crons, y Gemini (gratis) a veces se satura.
// Cada workflow se salta solo si su trabajo ya esta hecho, y su grupo de concurrencia lo pone en cola (nunca dos a la vez):
// - Shorts (social.yml) a las :10 de las 15, 19 y 23 UTC: social_publisher.py sabe que franjas ya publico.
// - Zibu (zibu-stories/daily.yml, auto=true) a las 9:35, 13:35 y 17:35 UTC: si hoy ya hay episodio, no hace nada.
// - Resumen nocturno (alerts.yml recap) a las 03:50 UTC de martes a sabado: el dia sale de la hora de Nueva York
//   (recap_day) y queda marcado, asi que no se repite con el de GitHub.
// - Video largo de YouTube (morning.yml) a las 06:50 y 11:50 UTC: se salta si hoy ya se subio.
// - Reddit (alerts.yml) domingo (reddit) y miercoles (midweek) a las 16:20 UTC: reddit_sent evita repetir.
// Cloudflare gratis admite 5 crons por worker: por eso se agrupan y se decide aqui por hora y dia (UTC).
const PIPE = "easymoneydecoded/yt-finance-pipeline";
const SOCIAL = { repo: PIPE, workflow: "social.yml" };
const ZIBU = { repo: "easymoneydecoded/zibu-stories", workflow: "daily.yml", inputs: { auto: "true" } };
const MORNING = { repo: PIPE, workflow: "morning.yml" };
const RECAP = { repo: PIPE, workflow: "alerts.yml", inputs: { mode: "recap" } };

function pick(cron, now) {
  const hour = now.getUTCHours(), day = now.getUTCDay();   // day: 0 = domingo
  switch (cron) {
    case "10 15,19,23 * * *": return SOCIAL;
    case "35 9,13,17 * * *":  return ZIBU;
    case "50 3,6,11 * * *":   return hour === 3 ? (day >= 2 && day <= 6 ? RECAP : null) : MORNING;
    case "20 16 * * 0,3":     return { repo: PIPE, workflow: "alerts.yml", inputs: { mode: day === 0 ? "reddit" : "midweek" } };
  }
  return null;
}

export default {
  async scheduled(event, env, ctx) {
    const job = pick(event.cron, new Date(event.scheduledTime));
    if (!job) return console.log(`sin trabajo para ${event.cron}`);
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
