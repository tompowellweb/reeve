# Host a Compose application

Reeve takes a Docker Compose project, builds or downloads its images, and runs it behind
the server's shared HTTPS proxy. The project can contain a web application, database and
supporting services. You choose one HTTP service to receive traffic for the site's hostname.

The process is: **prepare a folder → archive it → upload and review → deploy → verify**.
Prepare the folder on your computer or the application's existing server. Upload it through
the panel after [installing Reeve](install.md).

## 1. Prepare the project folder

A Compose file describes the application's containers: their images or build instructions,
settings, connections and storage. Start with the application's existing working Compose
setup or its author's Docker installation instructions. Reeve does not turn an arbitrary
source-code ZIP into a running application.

Keep everything needed to recreate the application inside one folder:

```text
my-application/
├── compose.yaml       # Required: the services and their configuration
├── .env               # If the Compose file uses environment variables
├── Dockerfile         # If a service builds its image from this folder
├── src/               # Source and dependency files needed by that build
├── uploads/           # Existing application files, if mounted from this folder
└── dumps/
    └── db.sql         # Optional database export; "db" is the Compose service name
```

Only `compose.yaml` is always required. Include the other files when the application uses
them. There is no separate Reeve manifest. Accepted Compose filenames are `compose.yaml`,
`compose.yml`, `docker-compose.yaml` and `docker-compose.yml`; include exactly one at the
project root.

For each service, either use an `image:` the server can pull, or supply `build:` with its
Dockerfile and build context. The build context is the directory Docker reads when building;
include everything the Dockerfile copies, including dependency lockfiles. A Dockerfile is
unnecessary for a service using a ready-made registry image. Image tarballs from `docker save`
are not an input format.

For example, a custom service might use:

```yaml
services:
  web:
    build:
      context: ./app
      dockerfile: Dockerfile
```

That requires `app/Dockerfile` and its build inputs inside the package. Local build contexts
support Dockerfile selection, build arguments and build targets; remote or additional build
contexts are not supported. Private images require registry access on the destination server;
there is no registry-login field in the import form.

### Settings and storage

Include `.env` and any files referenced by `env_file:`. Variables available only in your
current shell will not travel with the archive. Use literal image references and relative
file paths; variables in image names or mounted/input paths are rejected during review.

Set the application's public URL, allowed hostnames and proxy settings for its destination,
such as `https://shop.example.com`. The selected service must listen for HTTP on the container's
network interface, normally `0.0.0.0`, rather than only on its own loopback address.

Use project-relative mounts such as `./uploads:/app/uploads`, and include the directory even
if it is empty. Named volumes such as `db_data:/var/lib/mysql` are created on the new server;
their contents are **not** included just because they are named in Compose. Export databases
as described below. Copy other existing volume data into a project folder and mount that
folder, or provide the application's documented restore procedure.

Declare storage for every persistent path, including volumes declared by an image. Reeve
rejects images with undeclared storage so it cannot silently fall outside site backups.
Do not include raw database data directories, Docker's storage directory or the Docker socket.

## 2. Worked example: WordPress with MariaDB

This example uses registry images, so it needs no Dockerfile. For a new site, create an empty
folder for WordPress files:

```sh
mkdir -p wordpress-project/html
cd wordpress-project
```

Save this as `compose.yaml`:

```yaml
services:
  web:
    image: wordpress:php8.3-apache
    restart: unless-stopped
    ports:
      - "127.0.0.1:8080:80"
    environment:
      WORDPRESS_DB_HOST: db:3306
      WORDPRESS_DB_NAME: wordpress
      WORDPRESS_DB_USER: wordpress
      WORDPRESS_DB_PASSWORD: ${DB_PASSWORD}
    volumes:
      - ./html:/var/www/html
    depends_on:
      - db

  db:
    image: mariadb:11.8
    restart: unless-stopped
    environment:
      MARIADB_DATABASE: wordpress
      MARIADB_USER: wordpress
      MARIADB_PASSWORD: ${DB_PASSWORD}
      MARIADB_ROOT_PASSWORD: ${DB_ROOT_PASSWORD}
    volumes:
      - db_data:/var/lib/mysql

volumes:
  db_data:
```

Save this as `.env`, replacing both placeholder values with different generated passwords:

```dotenv
DB_PASSWORD=replace-with-an-application-password
DB_ROOT_PASSWORD=replace-with-a-different-root-password
```

The web container connects to `db` using the application account. Reeve needs the root
password and database name to perform database dumps and restores. These settings follow
the [WordPress image](https://github.com/docker-library/docs/blob/master/wordpress/README.md)
and [MariaDB image](https://mariadb.com/docs/server/server-management/automated-mariadb-deployment-and-administration/docker-and-mariadb/mariadb-server-docker-official-image-environment-variables)
configuration.

For this project, enter **HTTP service: `web`** and **Container HTTP port: `80`** in Reeve.
`8080` is only the port for local testing. Reeve removes published port mappings and connects
its proxy directly to port `80` inside `web`.

For an existing WordPress site, use versions compatible with that site, copy its files into
`html/`, and include its database export as `dumps/db.sql`. Check any existing `wp-config.php`:
its database host, credentials and table prefix must match the imported database. Existing
configuration is not automatically rewritten to use the example's environment variables.
A domain change also requires WordPress's own URL migration procedure.

## 3. Include existing database data

Skip this step for a new, empty database. For a migration, export the application's actual
database from the source server; copying the Compose file alone does not copy its records.
Pause application writes while taking the final database export and corresponding file copy.

Put one dump per database service in `dumps/`:

| Database image | Accepted dump | Example for a service named `db` |
| --- | --- | --- |
| MySQL or MariaDB | Plain SQL or gzip-compressed SQL | `dumps/db.sql` or `dumps/db.sql.gz` |
| PostgreSQL | Plain SQL or a custom-format `pg_dump` archive | `dumps/db.sql` or `dumps/db.dump` |

The filename uses the **Compose service name**, not the database name. If the service is
`database`, use `dumps/database.sql`. Keep unrelated files out of `dumps/`.

For a source stack using the MariaDB environment variables in the example, run this from
the source project's directory. Replace `/path/to/wordpress-project` with the package folder:

```sh
mkdir -p /path/to/wordpress-project/dumps
docker compose exec -T db sh -c \
  'MYSQL_PWD="$MARIADB_ROOT_PASSWORD" mariadb-dump --single-transaction --quick "$MARIADB_DATABASE"' \
  > /path/to/wordpress-project/dumps/db.sql
```

Check that the command succeeds and the dump contains the expected application tables.
For another source setup, use its normal export tool: `mysqldump`/`mariadb-dump` for a single
database, or `pg_dump -Fc` for a PostgreSQL `.dump`. Include routines or events if your
application uses them. Avoid all-server dumps, system databases and exports selecting a
different database name.

The destination database service needs its normal initialisation settings in `environment:`
or an included `env_file:`: a root password and database name for MySQL/MariaDB, or
`POSTGRES_PASSWORD` and the appropriate `POSTGRES_USER`/`POSTGRES_DB` for PostgreSQL.
File-based password variables alone are not sufficient for Reeve's dump integration.

Reeve recognises services using `mysql`, `mariadb` or `postgres` images without a custom build.
It starts those databases and imports supplied dumps before starting the full application.
Completed imports are recorded so a deployment retry does not import them again.

**Current backup limit:** scheduled database dumps and the fresh database dump in a complete
site backup require exactly one recognised database service with usable credentials. Projects
with multiple database services, custom database builds or other engines need their own
database backup and restore arrangements; do not assume file backups provide equivalent recovery.

## 4. Check and archive the folder

From the prepared project directory, validate the Compose configuration:

```sh
docker compose -f compose.yaml config --quiet
```

Test a fresh example or a disposable copy locally with `docker compose up -d`, inspect
`docker compose ps` and `docker compose logs`, and open `http://127.0.0.1:8080` for the example.
These commands do not perform Reeve's automatic `dumps/` import. A local migration test needs
the application's normal database restore procedure too.

If the original setup uses multiple Compose files, merge the intended deployment configuration
first. For example, run this in the source project and copy the output into the package as
its only Compose file, preserving referenced relative folders:

```sh
docker compose -f compose.yaml -f compose.production.yaml config \
  --no-path-resolution --output /tmp/reeve-prepared-compose.yaml
```

Review the result: paths must stay inside the package and required services must not depend
on enabling an optional profile. The output can contain resolved passwords. Docker documents
the merge and validation options in its [Compose configuration reference](https://docs.docker.com/reference/cli/docker/compose/config/).

Create the archive **outside** the project folder. From the folder containing `wordpress-project`:

```sh
tar -czf wordpress-project.tar.gz -C wordpress-project .
tar -tzf wordpress-project.tar.gz
```

The listing should include `compose.yaml`, `.env`, `html/` and any `dumps/` files.
Using `.` includes hidden files such as `.env`. ZIP is also accepted:

```sh
zip -r wordpress-project.zip wordpress-project
```

Reeve accepts files at archive root or inside one enclosing project folder. Limits are
512 MiB uploaded, 4 GiB expanded, 100,000 archive entries and 32 Compose services. Expanded
files and runtime data must also fit the site's disk quota. Remove unrelated caches, `.git`,
old backups and local tooling before packaging, while retaining everything the build needs.
The archive contains application secrets and possibly customer data; keep it private.

## 5. Upload, review and deploy

Open **Import application** on the panel's home page and fill in:

| Field | What to enter | Example |
| --- | --- | --- |
| Site name | A new name, up to 32 lowercase letters, numbers and hyphens | `shop` |
| Domain | The destination hostname, without a scheme or path | `shop.example.com` |
| HTTP service | The key under `services:` that handles web requests | `web` |
| Container HTTP port | The HTTP listening port inside that container | `80` |
| Project archive | The ZIP or tar you created | `wordpress-project.tar.gz` |

Choose a frontend or web proxy service if the application also has an API. One service is
routed directly; any further routing between frontend and API belongs in the application's
own configuration. Databases and worker services do not need public ports.

Click **Upload and review**. This saves and inspects the package; it does not run the
application. If it reports **Needs preparation**, fix the named issue in the source folder,
rebuild the archive and upload it again. A successful review checks package structure, not
whether images build or the application starts.

Click **Deploy application** after reviewing the service inventory. Reeve copies the project,
builds or pulls images, creates storage and private networks, imports supplied database dumps,
starts the application and checks its HTTP response. Watch the deployment status on the site
page. You can close the page and return later; builds can take several minutes.

## 6. Verify the application and backups

Open the site's hostname, sign in, check records from the old database and open an uploaded
file. For a new WordPress site, complete its initial setup. Confirm the final hostname and
HTTPS behaviour, including redirects and generated links.

The current Reeve proxy uses a local CA. Use the testing access described in
[Recover](recover.md#4-verify-before-switching-traffic), or complete the deployment's public
TLS and DNS setup before switching public traffic.

Take a complete site backup and check that the expected database dump is present. Connect
remote storage and rehearse a restore as described in [Operate](operate.md#backups-and-restores).
Keep the prepared project: application code and Compose image updates are operator-managed.
Import creates a new site; it is not an upload-over-existing-site update workflow.

## Common preparation and deployment problems

| Problem | What to check |
| --- | --- |
| No single Compose file found | Keep one recognised filename at archive root or inside one enclosing folder. Merge overrides first. |
| Missing file or outside-project path | Include the real file, use `./relative/paths`, and avoid symlinks in mounted or configuration paths. |
| Missing variable or unresolved image | Include `.env`/`env_file` inputs; write image names and file paths literally. |
| Rejected Compose option | Host namespaces, privileged mode, added capabilities, devices, external volumes/networks and custom network drivers are unsupported. Top-level `configs`, `secrets`, `include` and extension fields are also unsupported; use project files and ordinary service configuration. |
| Image declares storage absent from Compose | Add a named volume or project-relative mount for every storage path declared by the image. |
| Image pull or build fails | Check registry access, image architecture, Dockerfile inputs and build logs. The target server is amd64. |
| Database import fails | Match the dump filename to the service, use the right engine/format and verify credentials and the target database name. |
| HTTP check fails | Check the selected service and internal port, startup logs, allowed hostname and a non-loopback listening address. `/` must answer without a 4xx or 5xx response. On a public server the check waits up to two and a half minutes for the name's certificate to be issued; a name that does not point here yet is served with the edge's own certificate and still passes. |

Published ports and container names are replaced by Reeve's private routing and naming.
Other unsupported settings can be discovered during deployment even after structural review.
Read the failure before choosing **Retry deployment**; retries use the saved package, so
they cannot pick up corrections made to a local archive.
