"""Corta un lote temprano si detecta un fallo sistemático.

Motivación: el 2026-08-31, un cambio de UI en Sunu rompió la búsqueda de
pacientes y el bot pasó 2 días completos reintentando y fallando el 100%
de los pacientes antes de que alguien lo notara (no había ninguna señal
que distinguiera "un paciente puntual falló" de "todo el módulo está roto").
Este breaker corta la ejecución apenas ve N fallos *consecutivos* con la
misma causa, en vez de agotar el lote completo primero.
"""

from __future__ import annotations


class CircuitBreaker:
    def __init__(self, umbral: int = 5):
        self.umbral = umbral
        self._errores: list[str] = []

    def registrar(self, ok: bool, causa: str | None = None) -> str | None:
        """Registra un resultado (ok/fallo + causa). Si se debe abortar el
        lote, retorna un mensaje de alerta listo para loguear/enviar;
        si no, retorna None.
        """
        if ok:
            self._errores.clear()
            return None

        self._errores.append(causa or "error desconocido")
        if len(self._errores) >= self.umbral and len(set(self._errores)) == 1:
            return (
                f"Fallo sistemático detectado: {len(self._errores)} pacientes "
                f"consecutivos fallaron con la misma causa ('{self._errores[0]}'). "
                "Es probable que Sunu, MirSpiro o la red hayan cambiado algo que "
                "rompió el flujo, no que sean pacientes puntuales. Se abortó el "
                "resto del lote para no perder horas reintentando en vano."
            )
        return None
