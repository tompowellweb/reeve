set -eu
test -t 0
printf 'SSH_TTY_OK\n'
id -u
pwd
hosting-toolbox-proof
7z i | head -8
php -r 'echo "PHP ".PHP_VERSION."\n";'
composer --version --no-ansi
test ! -e /run/docker.sock
test ! -e /srv/sites
test ! -w /etc/passwd
mkdir -p /site/wp-content/themes/odd-layout
cd /site/wp-content/themes/odd-layout
printf '{"name":"hosting/odd-theme","require":{"psr/log":"^3.0"}}\n' > composer.json
if ! php -r 'exit(gethostbyname("repo.packagist.org")==="repo.packagist.org" ? 1 : 0);'; then
    printf 'PACKAGIST_DNS_UNAVAILABLE: using the official GitHub VCS repository for this fixture\n'
    printf '{"name":"hosting/odd-theme","require":{"psr/log":"3.0.2"},"repositories":[{"type":"vcs","url":"https://github.com/php-fig/log"},{"packagist.org":false}]}\n' > composer.json
fi
composer install --prefer-dist --no-interaction --no-progress --no-ansi
php -r 'require "vendor/autoload.php"; echo "NESTED_COMPOSER_OK ".getcwd()." uid=".posix_geteuid()."\n";'
php -r '$p=new PDO("pgsql:host=db;dbname=site",getenv("DATABASE_USER"),getenv("DATABASE_PASSWORD")); echo "DATABASE_ROWS ".$p->query("SELECT COUNT(*) FROM m23_import")->fetchColumn()."\n";'
printf 'written through the toolbox SSH session\n' > ssh-proof.txt
printf 'SSH_TOOLBOX_ACCEPTED\n'

exit
