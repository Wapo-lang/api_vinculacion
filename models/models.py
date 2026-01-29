from asyncio import exceptions
from odoo import models, fields, api
from datetime import datetime, timedelta
from odoo.exceptions import ValidationError
from odoo.exceptions import UserError
import requests
import logging


_logger = logging.getLogger(__name__)

TOKEN = "d7b58bfda5a69833a8263e5a6f2c2746814f4033"
BASE_API = "http://127.0.0.1:8000/api/"
HEADERS = {
    'Authorization': f'Token {TOKEN}',
    'Content-Type': 'application/json'
}

class Autor(models.Model):
    _name = 'biblioteca.autor'
    _description = 'Gestión de Autores'
    _rec_name = 'display_name'

    firstname = fields.Char(string='Nombre', required=True)
    lastname = fields.Char(string='Apellido', required=True)
    biografia = fields.Text(string='Biografía') 
    display_name = fields.Char(string='Nombre Completo', compute='_compute_display', store=True)

    @api.depends('firstname', 'lastname')
    def _compute_display(self):
        for record in self:
            record.display_name = f"{record.firstname} {record.lastname}"

    def _sync_author_to_django(self):
        url_autores = f"{BASE_API}autores-api/"
        payload = {
            'nombre': self.firstname,
            'apellido': self.lastname,
            'bibliografia': self.biografia or ''
        }
        try:
            res = requests.get(url_autores, headers=HEADERS, timeout=5)
            if res.status_code == 200:
                autores_django = res.json()
                match = next((a for a in autores_django if a['nombre'].lower() == self.firstname.lower() 
                            and a['apellido'].lower() == self.lastname.lower()), None)
                if match:
                    requests.put(f"{url_autores}{match['id']}/", json=payload, headers=HEADERS, timeout=10)
                else:
                    requests.post(url_autores, json=payload, headers=HEADERS, timeout=10)
        except Exception as e:
            _logger.error(f"Error sincronizando autor: {e}")

    @api.model_create_multi
    def create(self, vals_list):
        records = super(Autor, self).create(vals_list)
        for record in records:
            record._sync_author_to_django()
        return records

    def write(self, vals):
        res = super(Autor, self).write(vals)
        if not self.env.context.get('skip_sync'):
            for record in self:
                record._sync_author_to_django()
        return res

class Libro(models.Model):
    _name = 'biblioteca.libro'
    _description = 'Gestión de Libros'
    _rec_name = 'firstname'
    
    firstname = fields.Char(string='Nombre Libro', required=True)
    author = fields.Many2one('biblioteca.autor', string='Autor Libro')
    isbn = fields.Char(string='ISBN')
    description = fields.Text(string='Descripción')
    openlibrary_description = fields.Text(string='Estado de Sincronización', readonly=True)
    value = fields.Integer(string='Número de Ejemplares', default=1)
    ejemplares_disponibles = fields.Integer(string='Ejemplares Disponibles', default=1)

    # --- LÓGICA DE BÚSQUEDA OPEN LIBRARY ---
    @api.onchange('isbn')
    def _onchange_isbn_logic(self):
        if not self.isbn or len(self.isbn) < 10: return
        search_isbn = self.isbn.strip()
        
        # 1. Intentar cargar desde Django primero
        try:
            res = requests.get(f"{BASE_API}libros-api/{search_isbn}/", headers=HEADERS, timeout=5)
            if res.status_code == 200:
                data = res.json()
                self.firstname = data.get('titulo')
                self.description = data.get('descripcion')
                self.openlibrary_description = "Cargado desde Django (Ya existía)."
                return
        except: pass

        # 2. Buscar en Open Library
        ol_url = f"https://openlibrary.org/api/books?bibkeys=ISBN:{search_isbn}&format=json&jscmd=data"
        try:
            response = requests.get(ol_url, timeout=10)
            data = response.json()
            key = f"ISBN:{search_isbn}"
            
            if key in data:
                info = data[key]
                self.firstname = info.get('title')
                self.description = str(info.get('notes', info.get('description', '')))
                
                if info.get('authors'):
                    author_info = info['authors'][0]
                    # Extraer la ID del autor de la URL (ej: /authors/OL26320A/J.K._Rowling)
                    ol_author_id = author_info.get('url', '').split('/')[-2] if 'url' in author_info else None
                    
                    bio_texto = ""
                    if ol_author_id:
                        # Petición extra para la biografía del autor
                        res_bio = requests.get(f"https://openlibrary.org/authors/{ol_author_id}.json", timeout=5)
                        if res_bio.status_code == 200:
                            raw_bio = res_bio.json().get('bio', '')
                            bio_texto = raw_bio.get('value', raw_bio) if isinstance(raw_bio, dict) else raw_bio

                    full_name = author_info.get('name')
                    parts = full_name.split(' ', 1)
                    nom = parts[0]
                    ape = parts[1] if len(parts) > 1 else '.'
                    
                    self._get_or_create_local_author(nom, ape, bio_texto)
                    self.openlibrary_description = "Obtenido de Open Library (Libro + Bio)."
        except Exception as e:
            self.openlibrary_description = f"Error: {str(e)}"

    def _get_or_create_local_author(self, nom, ape, bio=""):
        autor_obj = self.env['biblioteca.autor'].search([('firstname','=',nom),('lastname','=',ape)], limit=1)
        if not autor_obj:
            autor_obj = self.env['biblioteca.autor'].create({
                'firstname': nom, 
                'lastname': ape,
                'biografia': bio
            })
        elif bio and not autor_obj.biografia:
            autor_obj.write({'biografia': bio})
        self.author = autor_obj.id

    # --- SINCRONIZACIÓN A DJANGO ---
    def _sync_to_django(self):
        url_libros = f"{BASE_API}libros-api/"
        django_author_id = self._get_or_create_django_author()
        
        payload = {
            'titulo': self.firstname,
            'isbn': self.isbn,
            'descripcion': self.description or '',
            'cantidad_total': self.value,
            'ejemplares_disponibles': self.ejemplares_disponibles,
            'autor': django_author_id
        }

        try:
            response = requests.post(url_libros, json=payload, headers=HEADERS, timeout=10)
            if response.status_code == 400:
                url_put = f"{url_libros}{self.isbn}/"
                response = requests.put(url_put, json=payload, headers=HEADERS, timeout=10)
            
            if response.status_code in [200, 201, 204]:
                self.with_context(skip_sync=True).write({
                    'openlibrary_description': 'Sincronizado con Django correctamente ✅'
                })
        except Exception as e:
            _logger.error(f"Error sincronizando libro con Django: {e}")

    def _get_or_create_django_author(self):
        if not self.author: return None
        url_autores = f"{BASE_API}autores-api/"
        nom = self.author.firstname.strip()
        ape = (self.author.lastname or '').strip() or '.'
        
        try:
            res = requests.get(url_autores, headers=HEADERS, timeout=10)
            if res.status_code == 200:
                autores = res.json()
                match = next((a for a in autores if 
                            a['nombre'].lower() == nom.lower() and 
                            a['apellido'].lower() == ape.lower()), None)
                if match: return match['id']
            
            payload = {'nombre': nom, 'apellido': ape, 'bibliografia': self.author.biografia or ''}
            new_auth_res = requests.post(url_autores, json=payload, headers=HEADERS, timeout=10)
            return new_auth_res.json().get('id') if new_auth_res.status_code in [200, 201] else None
        except: return None

    # --- ACCIONES ---
    def action_prestar(self):
        for record in self:
            if record.ejemplares_disponibles > 0:
                record.ejemplares_disponibles -= 1
            else:
                raise exceptions.UserError("No hay ejemplares disponibles.")

    def action_devolver(self):
        for record in self:
            if record.ejemplares_disponibles < record.value:
                record.ejemplares_disponibles += 1
            else:
                raise exceptions.UserError("Stock lleno.")

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        for record in records:
            record._sync_to_django()
        return records
    
    def write(self, vals):
        res = super(Libro, self).write(vals)
        if not self.env.context.get('skip_sync'):
            for record in self:
                record._sync_to_django()
        return res
          
class BibliotecaMulta(models.Model):
    _name = 'biblioteca.multa'
    _description = 'Gestión de Multas'

    prestamo_id = fields.Many2one('biblioteca.prestamo', string="Préstamo", required=True)
    usuario = fields.Many2one('biblioteca.usuario', string='Usuario', related='prestamo_id.usuario', store=True, readonly=True)
    monto = fields.Float(string='Valor a pagar')
    tipo_multa = fields.Selection(related='prestamo_id.tipo_multa', string='Tipo de multa', store=True, readonly=True)
    descripcion = fields.Char(string="Detalle multa")
    pago = fields.Selection(selection=[('pendiente','Pendiente'), ('saldada','Saldada')], string='Pago de la multa')

    motivo = fields.Selection(selection=[
        ('perdida','Pérdida'),
        ('retraso','Retraso'),
        ('daño','Daño'),
        ('robo', 'Robo'),
        ('otros','Otros')
    ], string='Causa de la multa')

    
class BibliotecaUsuario(models.Model):
    _name = 'biblioteca.usuario'
    _description = 'Gestión de Usuarios de la Biblioteca'
    _rec_name = 'nombre_completo'

    nombre = fields.Char(string='Nombre', required=True)
    apellido = fields.Char(string='Apellido', required=True)
    cedula = fields.Char(string='Cédula', required=True)
    telefono = fields.Char(string='Teléfono')
    correo = fields.Char(string='Correo electrónico')
    direccion = fields.Char(string='Dirección')
    nombre_completo = fields.Char(string='Nombre completo', compute='_compute_nombre_completo', store=True)
    fecha_vencimiento = fields.Date(String ='Estado de la membresía')
    estado_membresia = fields.Selection([
        ('activa', 'Activa'),
        ('vencida', 'Vencida'),
        ('inactiva', 'Suspendida')
        ], string = 'Estado de la Membresía', compute = '_compute_estado_membresia', store = True)
        
    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if 'fecha_vencimiento' not in vals:
                fecha_actual = fields.Date.context_today(self)
                vals['fecha_vencimiento'] = fecha_actual + timedelta(days=6*30)
        return super().create(vals_list)

    
    @api.depends('fecha_vencimiento')
    def _compute_estado_membresia(self):
        hoy = fields.Date.today()
        for rec in self:
            if not rec.fecha_vencimiento:
                rec.estado_membresia = 'inactiva'
            elif rec.fecha_vencimiento >= hoy:
                rec.estado_membresia = 'activa'
            else:
                rec.estado_membresia = 'vencida'

    @api.depends('nombre', 'apellido')
    def _compute_nombre_completo(self):
        for record in self:
            record.nombre_completo = f"{record.nombre} {record.apellido}"

    @api.constrains('correo')
    def _check_correo(self):
        for rec in self:
            if rec.correo and '@' not in rec.correo:
                raise ValidationError('Ingrese un correo electrónico válido.')

    @api.constrains('cedula')
    def _check_cedula(self):
        for rec in self:
            # Puedes reutilizar el validador de cédula de tu clase CedulaEcuador
            valido, msg = CedulaEcuador._validar_cedula_ecuador(rec.cedula or '')
            if not valido:
                raise ValidationError(f'Cédula: {msg}')

class BibliotecaPrestamos(models.Model):
    _name = 'biblioteca.prestamo'
    _description = 'Modelo de manejo de prestamos'
    _rec_name = 'fecha_max'

    name = fields.Char(required=True)
    usuario = fields.Many2one('biblioteca.usuario', string='Usuario', required=True,
                              default=lambda self: self._default_usuario())
    fecha_prestamo = fields.Datetime(default=datetime.now(), string='Fecha de préstamo')
    libro = fields.Many2one('biblioteca.libro', string='Título de libro')
    fecha_devolucion = fields.Datetime()
    multa_bol = fields.Boolean(default=False)
    multa = fields.Float()
    estado = fields.Selection([
        ('b', 'Borrador'),
        ('p', 'Prestado'),
        ('m', 'Multa'),
        ('d', 'Devuelto')
    ], string='Estado', default='b')
    personal = fields.Many2one('res.users', string='Persona que prestó el libro',
                               default=lambda self: self.env.uid)
    fecha_max = fields.Datetime(compute='_compute_fecha_devo', string='Fecha Máxima de devolución')

    tipo_multa = fields.Selection(
        selection=[('perdida', 'Pérdida'),
                   ('retraso', 'Retraso'),
                   ('daño', 'Daño'),
                   ('robo', 'Robo'),
                   ('otros', 'Otros')],
        string='Tipo de multa')

    multa_otro_tipo = fields.Char(string='Especificar tipo de multa')

    @api.model
    def _default_usuario(self):
        usuario = self.env['biblioteca.usuario'].search([('correo', '=', self.env.user.email)], limit=1)
        return usuario.id if usuario else False

    @api.onchange('tipo_multa')
    def _onchange_tipo_multa(self):
        valores_tipos = {
            'perdida': 30.0,
            'retraso': 10.0,
            'daño': 25.0,
            'robo': 20.0,
            'otros': 0.0,
        }
        if self.tipo_multa in valores_tipos:
            self.multa = valores_tipos[self.tipo_multa]
        else:
            self.multa = 0.0
        if self.tipo_multa != 'otros':
            self.multa_otro_tipo = False

    def write(self, vals):
        if 'name' not in vals or not vals['name']:
            seq = self.env.ref('biblioteca.sequence_codigo_prestamos').next_by_code('biblioteca.prestamo')
            vals['name'] = seq
        return super(BibliotecaPrestamos, self).write(vals)

    @api.model
    def create(self, vals):
        if isinstance(vals, list):
            for val in vals:
                if not val.get('name'):
                    val['name'] = self.env.ref('biblioteca.sequence_codigo_prestamos').next_by_code('biblioteca.prestamo')
            return super(BibliotecaPrestamos, self).create(vals)
        else:
            if not vals.get('name'):
                vals['name'] = self.env.ref('biblioteca.sequence_codigo_prestamos').next_by_code('biblioteca.prestamo')
            return super(BibliotecaPrestamos, self).create(vals)

    def generar_prestamo(self):
        libro = self.libro
        if libro.ejemplares_disponibles <= 0:
            raise UserError(f"El libro '{libro.firstname}' no se encuentra en stock.")
        else:
            libro.ejemplares_disponibles -= 1
            libro.write({'ejemplares_disponibles': libro.ejemplares_disponibles})
            self.write({'estado': 'p'})  # marcar como prestado


    @api.depends('fecha_prestamo')
    def _compute_fecha_devo(self):
        for record in self:
            record.fecha_max = record.fecha_prestamo + timedelta(days=2)

    def _cron_multas(self):
        prestamos = self.env['biblioteca.prestamo'].search([
            ('estado', '=', 'p'),
            ('fecha_max', '<', datetime.now())
        ])
        for prestamo in prestamos:
            prestamo.write({'estado': 'm', 'multa_bol': True, 'multa': 1.0})
        prestamos_con_multa = self.env['biblioteca.prestamo'].search([('estado', '=', 'm')])
        
        for prestamo in prestamos_con_multa:
            days = (datetime.now() - prestamo.fecha_max).days
            prestamo.write({'multa': days})

    def asignar_multa(self):
        multa_model = self.env['biblioteca.multa']

        if not self.tipo_multa:
            raise UserError("Debe seleccionar el tipo de multa para asignar.")

        multas_existentes = multa_model.search([
            ('prestamo_id', '=', self.id),
            ('motivo', '=', self.tipo_multa)
        ])
        if multas_existentes:
            raise UserError("Ya existe una multa asignada con este motivo para este préstamo.")

        multa_model.create({
            'prestamo_id': self.id,
            'monto': self.multa,    
            'motivo': self.tipo_multa,
            'descripcion': self.multa_otro_tipo if self.tipo_multa == 'otros' else '',
            'pago': 'pendiente',
        })
        self.write({'estado': 'm', 'multa_bol': True})
        return True
    
    def devolver_libro(self):
        self.write({'estado': 'd', 'fecha_devolucion': fields.Datetime.now()})
        libro = self.libro
        if libro and libro.ejemplares_disponibles is not None:
            libro.ejemplares_disponibles += 1
            libro.write({'ejemplares_disponibles': libro.ejemplares_disponibles})



class CedulaEcuador(models.Model):
    _name = 'biblioteca.cedula'
    _description = 'Verificador de Cédula Ecuatoriana'
    _rec_name = 'cedula'

    cedula = fields.Char(string='Cédula', required=True)
    es_valida = fields.Boolean(string='Cédula válida', compute='_compute_validez', store=True)
    mensaje = fields.Char(string='Mensaje de validación', compute='_compute_validez', store=True)

    @api.depends('cedula')
    def _compute_validez(self):
        for rec in self:
            valido, msg = self._validar_cedula_ecuador(rec.cedula or '')
            rec.es_valida = valido
            rec.mensaje = msg

    @staticmethod
    def _validar_cedula_ecuador(cedula: str):
        cedula = (cedula or '').strip()
        if not cedula.isdigit():
            return False, "La cédula debe contener solo dígitos."
        if len(cedula) != 10:
            return False, "La cédula debe tener 10 dígitos."
        prov = int(cedula[:2])
        if prov < 1 or prov > 24:
            return False, "Código de provincia inválido."
        tercer = int(cedula[2])
        if tercer >= 6:
            return False, "Tercer dígito inválido para cédula natural."
        digitos = list(map(int, cedula))
        coef = [2, 1, 2, 1, 2, 1, 2, 1, 2]
        total = 0
        for i in range(9):
            prod = digitos[i] * coef[i]
            if prod >= 10:
                prod -= 9
            total += prod
        dig_verificador = (10 - (total % 10)) % 10
        if dig_verificador == digitos[9]:
            return True, "Cédula válida."
        return False, "Dígito verificador inválido."

    @api.constrains('cedula')
    def _check_cedula(self):
        for rec in self:
            valido, msg = self._validar_cedula_ecuador(rec.cedula or '')
            if not valido:
                raise ValidationError(msg)