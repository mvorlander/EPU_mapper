// Compatibility for an older server that is still running during the upgrade.
const notice=document.createElement('div');
notice.style.cssText='padding:16px;background:#fff3cd;color:#664d03;font-weight:600';
notice.textContent='Particle density has moved into the main acquisition dashboard. Stop and restart the mapper, then reimport your .cs files. A browser refresh alone cannot update the running server.';
document.querySelector('header').after(notice);
