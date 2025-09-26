#!/usr/bin/env python3
"""
Script pour extraire les valeurs FQDN des fichiers YAML/YML dans le dossier apps/
et générer un fichier gatus-config.yml compatible avec Gatus pour les applications Cedille
"""

import os
import yaml
import glob
from pathlib import Path
from datetime import datetime


def find_fqdn_in_yaml(file_path):
    """
    Extrait les valeurs FQDN d'un fichier YAML (supporte les multi-documents)
    """
    # Ignorer les fichiers templates Helm qui causent des erreurs de parsing
    if "/templates/" in str(file_path) or "\\templates\\" in str(file_path):
        return []

    fqdns = []
    try:
        with open(file_path, "r", encoding="utf-8") as file:
            # Utiliser yaml.safe_load_all pour gérer les multi-documents
            documents = yaml.safe_load_all(file)
            for doc in documents:
                if doc:  # Ignorer les documents vides
                    # Contexte: priorité plus élevée pour ingress et kustomization
                    is_ingress_context = "ingress.yaml" in str(file_path).lower()
                    is_kustomization_context = (
                        "kustomization.yaml" in str(file_path).lower()
                    )

                    fqdns.extend(
                        extract_fqdn_recursive(
                            doc, is_ingress_context, is_kustomization_context
                        )
                    )
    except Exception as e:
        print(f"Erreur lors de la lecture de {file_path}: {e}")

    return fqdns


def is_valid_fqdn(fqdn):
    """
    Vérifie si un FQDN est valide pour la surveillance (tout domaine légitime avec Ingress)
    """
    # Exclure les adresses email (contiennent @)
    if "@" in fqdn:
        return False

    # Exclure les URLs de registres Docker
    docker_registries = ["ghcr.io", "docker.io", "registry.k8s.io", "quay.io"]
    for registry in docker_registries:
        if fqdn.startswith(registry):
            return False

    # Patterns invalides à exclure
    invalid_patterns = [
        "example.com",
        "example.local",
        "chart-example.local",
        "localhost",
        "127.0.0.1",
        "0.0.0.0",
        ".local",
        "example.org",
        "test.com",
    ]

    # Vérifier si le FQDN contient des patterns invalides
    fqdn_lower = fqdn.lower()
    for pattern in invalid_patterns:
        if pattern in fqdn_lower:
            return False

    # Vérifier si c'est un vrai domaine (contient au moins un point et pas de variables Helm/Kubernetes)
    if (
        "." not in fqdn
        or "{" in fqdn
        or "}" in fqdn
        or fqdn.startswith("{{")
        or "$(" in fqdn
    ):
        return False

    # Si c'est dans un fichier ingress.yaml ou kustomization.yaml, c'est probablement valide
    # Tous les domaines avec une configuration Ingress sont des endpoints web légitimes
    return True


def extract_fqdn_recursive(
    obj, is_ingress_context=False, is_kustomization_context=False
):
    """
    Recherche récursivement les clés contenant des FQDNs dans un objet YAML
    Cherche: 'host', 'hosts', 'dnsNames', 'commonName', 'value' (pour kustomization patches)
    """
    fqdns = []

    if isinstance(obj, dict):
        for key, value in obj.items():
            # Clés qui contiennent directement des FQDNs dans les ingress Kubernetes
            if key in ["host", "commonName"] and isinstance(value, str):
                if (
                    "." in value
                    and not value.startswith("http")
                    and is_valid_fqdn(value)
                ):
                    fqdns.append(value)
            # Clés qui contiennent des listes de FQDNs (tls.hosts)
            elif key in ["hosts", "dnsNames"] and isinstance(value, list):
                for item in value:
                    if (
                        isinstance(item, str)
                        and "." in item
                        and not item.startswith("http")
                        and is_valid_fqdn(item)
                    ):
                        fqdns.append(item)
            # Pour les kustomization files, chercher dans les patches avec 'value'
            elif key == "value" and isinstance(value, str) and is_kustomization_context:
                if (
                    "." in value
                    and not value.startswith("http")
                    and is_valid_fqdn(value)
                ):
                    fqdns.append(value)
            # Pour les kustomization files, chercher dans les patches inline (patch: |-)
            elif key == "patch" and isinstance(value, str) and is_kustomization_context:
                # Parser les lignes du patch pour trouver les lignes "value: ..."
                for line in value.split("\n"):
                    line = line.strip()
                    if line.startswith("value: "):
                        potential_fqdn = line.replace("value: ", "").strip()
                        if (
                            "." in potential_fqdn
                            and not potential_fqdn.startswith("http")
                            and is_valid_fqdn(potential_fqdn)
                        ):
                            fqdns.append(potential_fqdn)
            # Chercher aussi dans les paths pour les patches kustomization
            elif key == "path" and isinstance(value, str):
                # Ignorer les paths de structure JSON/YAML, ne pas extraire de FQDN
                pass
            else:
                # Continuer la recherche récursive
                fqdns.extend(
                    extract_fqdn_recursive(
                        value, is_ingress_context, is_kustomization_context
                    )
                )
    elif isinstance(obj, list):
        for item in obj:
            fqdns.extend(
                extract_fqdn_recursive(
                    item, is_ingress_context, is_kustomization_context
                )
            )

    return fqdns


def create_simple_endpoint(fqdn, app_name, source_file):
    """
    Crée un endpoint simple pour un FQDN donné
    """
    # Créer un nom unique basé sur le FQDN complet
    if "prodv2" in fqdn:
        # Pour les domaines prodv2, utiliser le sous-domaine + "-prod"
        subdomain = fqdn.split(".")[0]
        endpoint_name = f"{subdomain}-prod"
    else:
        # Pour les autres domaines, utiliser le sous-domaine
        subdomain = fqdn.split(".")[0]
        endpoint_name = subdomain

    endpoint = {
        "name": endpoint_name,
        "url": f"https://{fqdn}",
        "interval": "5m",
        "conditions": ["[STATUS] == 200", "[RESPONSE_TIME] < 3000"],
        "alerts": [{"type": "discord", "failure-threshold": 3, "success-threshold": 2}],
    }

    return endpoint


def main():
    """
    Fonction principale
    """
    # Chemin vers le dossier apps
    apps_dir = Path("apps")

    if not apps_dir.exists():
        print("Le dossier 'apps' n'existe pas dans le répertoire courant")
        return

    all_fqdns = []

    # Parcourir récursivement tous les fichiers YAML/YML dans apps/
    yaml_patterns = ["**/*.yaml", "**/*.yml"]

    for pattern in yaml_patterns:
        for yaml_file in apps_dir.glob(pattern):
            print(f"Analyse du fichier: {yaml_file}")
            fqdns = find_fqdn_in_yaml(yaml_file)

            for fqdn in fqdns:
                # Déterminer le nom de l'app depuis le chemin du fichier
                # apps/app-name/... ou apps/app-name/subfolder/...
                path_parts = yaml_file.parts
                app_name = "unknown"

                if len(path_parts) >= 2:
                    app_name = path_parts[1]  # apps/app-name/...

                    # Gérer les cas spéciaux comme dronolab/webApp
                    if len(path_parts) >= 3 and path_parts[2] not in ["base", "prod"]:
                        app_name = f"{path_parts[1]}-{path_parts[2]}"

                # Ajouter des métadonnées sur l'origine du FQDN
                endpoint_info = {
                    "fqdn": fqdn,
                    "source_file": str(yaml_file),
                    "app_name": app_name,
                }
                all_fqdns.append(endpoint_info)

    # Supprimer les doublons en gardant la première occurrence
    unique_fqdns = []
    seen_fqdns = set()

    for endpoint in all_fqdns:
        if endpoint["fqdn"] not in seen_fqdns:
            unique_fqdns.append(endpoint)
            seen_fqdns.add(endpoint["fqdn"])

    # Trier par FQDN pour un ordre cohérent
    unique_fqdns.sort(key=lambda x: x["fqdn"])

    # Créer la liste des endpoints
    endpoints_list = []
    for endpoint_info in unique_fqdns:
        endpoint = create_simple_endpoint(
            endpoint_info["fqdn"],
            endpoint_info["app_name"],
            endpoint_info["source_file"],
        )
        endpoints_list.append(endpoint)

    # Structure simple avec juste les endpoints
    endpoints_config = {
        "# Configuration générée automatiquement": f"Généré le {datetime.now().isoformat()}",
        "endpoints": endpoints_list,
    }

    # Écrire le fichier gatus-endpoints.yml
    with open("gatus-endpoints.yml", "w", encoding="utf-8") as f:
        yaml.dump(
            endpoints_config,
            f,
            default_flow_style=False,
            allow_unicode=True,
            sort_keys=False,
        )

    print(
        f"Fichier gatus-endpoints.yml généré avec {len(unique_fqdns)} endpoints uniques"
    )

    # Afficher un résumé
    print("\nEndpoints trouvés:")
    for endpoint in unique_fqdns:
        print(
            f"  - {endpoint['fqdn']} (app: {endpoint['app_name']}, source: {Path(endpoint['source_file']).name})"
        )

    print(f"\nFichier généré:")
    print(f"  - gatus-endpoints.yml (endpoints pour Gatus)")

    if not unique_fqdns:
        print(
            "⚠️  Aucun FQDN valide trouvé. Vérifiez que les fichiers contiennent des domaines .cedille.club ou .etsmtl.ca"
        )


if __name__ == "__main__":
    main()
