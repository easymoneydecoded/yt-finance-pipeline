// Despertador de respaldo para los Shorts: GitHub a veces no lanza sus crons (sobre todo en fin de semana).
// A las :10 de cada franja (15, 19 y 23 UTC) pide a GitHub que ejecute social.yml. Si GitHub ya publico esa
// franja, social_publisher.py lo sabe (history/social_slots.json) y no repite; el grupo de concurrencia evita
// que dos ejecuciones publiquen a la vez.
export default {
  async scheduled(event, env, ctx) {
    const r = await fetch(
      "https://api.github.com/repos/easymoneydecoded/yt-finance-pipeline/actions/workflows/social.yml/dispatches",
      {
        method: "POST",
        headers: {
          Authorization: `Bearer ${env.GH_TOKEN}`,
          Accept: "application/vnd.github+json",
          "User-Agent": "tickerpress-cron",
        },
        body: JSON.stringify({ ref: "main" }),
      },
    );
    console.log("dispatch social.yml:", r.status, await r.text());
  },
};
